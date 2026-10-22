from copy import deepcopy
import unittest
from tokenometer.models import UsageEvent, digest
from tokenometer.receipts import validate_receipt


def make_event(platform, counters, receipt):
    return UsageEvent(platform+':'+digest('event'),platform,digest('session'),None,*counters,counter_receipt=receipt)


def ref(kind):
    return {'source':digest('/fixture/source'),'kind':kind,'index':1,'retry':0}


class ReceiptTests(unittest.TestCase):
    def test_antigravity_maxima_and_reasoning_inclusion(self):
        receipt={'schema':'antigravity.protobuf.v1','source_refs':[ref('steps'),ref('gen_metadata')],
                 'source_timestamps':[None,None], 'observations':[
                     {'input':10,'cache_creation':3,'cache_read':100,'output_total':7,'visible_output':None,'reasoning':2},
                     {'input':10,'cache_creation':3,'cache_read':100,'output_total':None,'visible_output':5,'reasoning':3}]}
        event=make_event('antigravity',(10,100,3,8,3),receipt)
        validate_receipt(event)
        self.assertEqual(event.total,121)
        wrong=deepcopy(receipt);wrong['observations'][1]['reasoning']=4
        with self.assertRaises(ValueError):validate_receipt(make_event('antigravity',(10,100,3,8,3),wrong))

    def test_legacy_exact_six_field_evidence(self):
        fields=('input_tokens','cached_input_tokens','cache_write_input_tokens','output_tokens','reasoning_output_tokens','total_tokens')
        last=dict(zip(fields,(112,100,2,8,3,120)))
        baseline={k:v*3 for k,v in last.items()};current={k:v*4 for k,v in last.items()}
        receipt={'schema':'codex.legacy_delta.v1','source_refs':[ref('jsonl')],
                 'counters':last,'last':last.copy(),'baseline':baseline,'current':current}
        validate_receipt(make_event('codex',(10,100,2,8,3),receipt))
        receipt['current']['cached_input_tokens']+=1
        with self.assertRaises(ValueError):validate_receipt(make_event('codex',(10,100,2,8,3),receipt))

    def test_raw_content_or_noninteger_counter_cannot_enter_receipt(self):
        receipt={'schema':'antigravity.protobuf.v1','source_refs':[ref('steps')],
                 'source_timestamps':[None], 'observations':[{'input':0,'cache_creation':None,'cache_read':None,'output_total':0,'visible_output':None,'reasoning':None}]}
        for mutate in ('prompt','boolean','raw-reference'):
            bad=deepcopy(receipt)
            if mutate=='prompt':bad['prompt']='not permitted'
            elif mutate=='boolean':bad['observations'][0]['input']=True
            else:bad['source_refs'][0]['source']='raw/file/path'
            with self.assertRaises(ValueError):validate_receipt(make_event('antigravity',(0,0,0,0,0),bad))
