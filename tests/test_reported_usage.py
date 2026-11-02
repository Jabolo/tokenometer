import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone

from tokenometer.reported_usage import eligible_daily, load_reports, parse_report


def report_data():
    return {
        'chart_key': 'tokens_used', 'chart_title': 'Tokens',
        'summary': {'label': 'Tokens', 'value': 119704167, 'previous_value': 10, 'is_partial': False},
        'series': [{'column': 'All', 'source': 'model', 'summary': {
            'label': 'Tokens', 'value': 119704167, 'previous_value': 0, 'is_partial': False}}],
        'rows': [{'Start Time': '2026-11-01T00:00:00Z', 'End Time': '2026-11-02T00:00:00Z',
                  'All': 119704167, 'model a': 80060463, 'model b': 39643704}],
    }


def make_report(data=None, **changes):
    raw = json.dumps(data if data is not None else report_data()).encode()
    provenance = {'sha256': hashlib.sha256(raw).hexdigest(), 'product': 'codex', 'scope': 'a' * 64,
                  'start': '2026-11-01T00:00:00Z', 'end': '2026-11-02T00:00:00Z',
                  'timezone': 'UTC', 'captured_at': '2026-11-02T01:00:00Z', 'complete': True}
    provenance.update(changes)
    return parse_report(raw, provenance)


def eligible(reports, **changes):
    args = {'cutoff': '2026-11-02T00:00:00Z', 'codex_timestamps': [],
            'codex_has_conflicts': False, 'codex_has_ambiguous_timestamps': False}
    args.update(changes)
    return eligible_daily(reports, **args)


class ReportedUsageTests(unittest.TestCase):
    def test_private_manifest_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self.assertEqual(load_reports(root / 'missing'), [])
            data = json.dumps(report_data()).encode()
            provenance = {'sha256': hashlib.sha256(data).hexdigest(), 'product': 'codex', 'scope': 'a' * 64,
                          'start': '2026-11-01T00:00:00Z', 'end': '2026-11-02T00:00:00Z',
                          'timezone': 'UTC', 'captured_at': '2026-11-02T01:00:00Z', 'complete': True}
            report_file = root / 'source.json'; report_file.write_bytes(data)
            manifest = {'schema_version': 1, 'reports': [{'file': 'source.json', 'provenance': provenance}]}
            manifest_file = root / 'manifest.json'; manifest_file.write_text(json.dumps(manifest))
            self.assertEqual(eligible(load_reports(root)), [{'date': '2026-11-01', 'total': 119704167}])
            for name in ('../source.json', '/source.json', 'sub/source.json', 'manifest.json', '.hidden.json'):
                manifest['reports'][0]['file'] = name
                manifest_file.write_text(json.dumps(manifest))
                with self.subTest(name=name), self.assertRaises(ValueError): load_reports(root)
            manifest['reports'][0]['file'] = 'source.json'
            manifest['reports'].append(deepcopy(manifest['reports'][0]))
            manifest_file.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError): load_reports(root)
            manifest['reports'].pop()
            manifest_file.write_text(json.dumps(manifest))
            report_file.unlink(); report_file.symlink_to(root / 'other.json')
            with self.assertRaises(ValueError): load_reports(root)
            report_file.unlink(); report_file.write_bytes(data + b' ')
            with self.assertRaises(ValueError): load_reports(root)

    def test_private_manifest_missing_invalid_or_linked_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); reports = root / 'reports'; reports.mkdir()
            with self.assertRaises(ValueError): load_reports(reports)
            manifest = reports / 'manifest.json'; manifest.write_text('{"schema_version":1,"schema_version":1,"reports":[]}')
            with self.assertRaises(ValueError): load_reports(reports)
            manifest.write_text(json.dumps({'schema_version': True, 'reports': []}))
            with self.assertRaises(ValueError): load_reports(reports)
            manifest.write_text(json.dumps({'schema_version': 1, 'reports': [], 'unknown': 1}))
            with self.assertRaises(ValueError): load_reports(reports)
            manifest.write_text(json.dumps({'schema_version': 1, 'reports': []}))
            self.assertEqual(load_reports(reports), [])
            link = root / 'linked'; link.symlink_to(reports, target_is_directory=True)
            with self.assertRaises(ValueError): load_reports(link)
            manifest.write_bytes(b' ' * (1024 * 1024 + 1))
            with self.assertRaises(ValueError): load_reports(reports)
            manifest.unlink(); manifest.symlink_to(root / 'other.json')
            with self.assertRaises(ValueError): load_reports(reports)

    def test_original_current_view_shape_is_one_private_observation(self):
        report = make_report()
        self.assertEqual(eligible([report]), [{'date': '2026-11-01', 'total': 119704167}])
        self.assertEqual(eligible([report, report]), eligible([report]))
        self.assertNotIn('a' * 64, str(eligible([report])))

    def test_late_event_retracts_and_utc_boundaries_are_half_open(self):
        report = make_report()
        self.assertEqual(eligible([report], codex_timestamps=['2026-11-01T23:59:59Z']), [])
        self.assertEqual(eligible([report], codex_timestamps=['2026-11-02T00:00:00Z']), eligible([report]))
        self.assertEqual(eligible([report], cutoff='2026-11-01T23:59:59Z'), [])
        self.assertEqual(eligible([report], codex_timestamps=['2026-11-02T00:30:00+01:00']), [])

    def test_unknown_dates_and_conflicts_block_any_report(self):
        report = make_report()
        for args in ({'codex_timestamps': [None]}, {'codex_has_conflicts': True},
                     {'codex_has_ambiguous_timestamps': True}):
            with self.subTest(args=args): self.assertEqual(eligible([report], **args), [])
        with self.assertRaises(ValueError): eligible([report], codex_has_conflicts=None)
        with self.assertRaises(ValueError): eligible([report], codex_timestamps=['2026-11-01'])

    def test_revisions_and_unproven_account_scopes_are_not_added(self):
        first = make_report()
        changed = report_data()
        changed['summary']['value'] += 1
        changed['series'][0]['summary']['value'] += 1
        changed['rows'][0]['All'] += 1
        changed['rows'][0]['model a'] += 1
        self.assertEqual(eligible([first, make_report(changed)]), [])
        self.assertEqual(eligible([first, make_report(scope='b' * 64)]), [])
        with self.assertRaises(ValueError): make_report(product='work')
        with self.assertRaises(ValueError): make_report(product='chat')

    def test_missing_partial_counter_mismatch_and_bad_types_are_rejected(self):
        corruptions = []
        for path, value in [(('summary', 'is_partial'), True), (('summary', 'value'), None),
                            (('series', 0, 'summary', 'is_partial'), True),
                            (('rows', 0, 'All'), True), (('rows', 0, 'model a'), 0),
                            (('rows', 0, 'End Time'), '2026-11-01T23:00:00Z'),
                            (('summary', 'value'), 2**53)]:
            data = report_data(); target = data
            for part in path[:-1]: target = target[part]
            target[path[-1]] = value; corruptions.append(data)
        for data in corruptions:
            with self.subTest(data=data), self.assertRaises(ValueError): make_report(data)
        with self.assertRaises(ValueError): make_report(complete=False)
        with self.assertRaises(ValueError): make_report(captured_at='2026-11-01T22:00:00Z')
        with self.assertRaises(ValueError): make_report(sha256='0' * 64)
        with self.assertRaises(ValueError): make_report(timezone='Europe/Warsaw')

    def test_duplicate_json_keys_rejected_even_if_digest_matches(self):
        raw = b'{"chart_key":"tokens_used","chart_key":"tokens_used"}'
        p = {'sha256': hashlib.sha256(raw).hexdigest(), 'product': 'codex', 'scope': 'a' * 64,
             'start': '2026-11-01T00:00:00Z', 'end': '2026-11-02T00:00:00Z',
             'timezone': 'UTC', 'captured_at': '2026-11-02T01:00:00Z', 'complete': True}
        with self.assertRaises(ValueError): parse_report(raw, p)

    def test_multi_day_requires_full_contiguous_coverage_and_summary(self):
        data = report_data()
        second = deepcopy(data['rows'][0]); second['Start Time'] = '2026-11-02T00:00:00Z'; second['End Time'] = '2026-11-03T00:00:00Z'
        data['rows'].append(second); data['summary']['value'] *= 2; data['series'][0]['summary']['value'] *= 2
        report = make_report(data, end='2026-11-03T00:00:00Z', captured_at='2026-11-03T01:00:00Z')
        self.assertEqual(len(eligible([report], cutoff='2026-11-03T00:00:00Z')), 2)
        data['rows'][1]['Start Time'] = '2026-11-03T00:00:00Z'
        with self.assertRaises(ValueError): make_report(data, end='2026-11-03T00:00:00Z', captured_at='2026-11-03T01:00:00Z')


if __name__ == '__main__':
    unittest.main()
