import unittest

from ..baseline import BaselineRunner
from ..baseline.classification import OutputPercentileClassifier
from ..baseline.models import EndpointView
from ..runtime import PercentileReference
from .test_baseline import config, endpoint, req


class DynamicClassificationTests(unittest.TestCase):
    def reference(self):
        return PercentileReference.from_lengths(list(range(1,11)))

    def settings(self, mode="percentile_dynamic", **overrides):
        return config(**(dict(batch_scope="all", output_classification=mode, batch_order="light_first_fifo") | overrides))

    def test_pressure_drives_monotone_threshold_levels(self):
        settings = self.settings()
        policy = OutputPercentileClassifier(settings,self.reference())
        observed = []
        for i,(occupied,ready) in enumerate(((0,0),(6,0),(9,0),(10,10))):
            view = EndpointView("a",1000,1000000,10,0,0,occupied)
            decisions,snapshot = policy.classify_batch((req(i,1,8),),(view,),ready,i,i)
            observed.append(snapshot["threshold"])
        self.assertEqual(observed,[.9,.8,.7,.6])
        self.assertEqual(snapshot["projected_excess_requests"],11)

    def test_fixed_threshold_and_reference_are_unchanged(self):
        settings = self.settings("percentile_fixed")
        reference = self.reference()
        policy = OutputPercentileClassifier(settings,reference)
        for occupied in (0,10):
            decisions,snapshot = policy.classify_batch((req(0,1,8),),
                (EndpointView("a",1000,1000000,10,0,0,occupied),),100,0,0)
            self.assertEqual(snapshot["threshold"],.8)
            self.assertTrue(decisions["0"]["output_heavy"])
            self.assertEqual(decisions["0"]["output_token_cutoff"],8)
        self.assertEqual(reference.sample_count,10)
        self.assertFalse(reference.values.flags.writeable)

    def test_input_rule_or_and_fifo_partition(self):
        settings = self.settings("percentile_fixed",batch_size=4)
        policy = OutputPercentileClassifier(settings,self.reference())
        requests = [req(0,1,9),req(1,1,7),req(2,1,2),req(3,10,1)]
        result = BaselineRunner(settings,classification_policy=policy).run(requests)
        self.assertEqual(result.batches[0]["dispatch_order"],["1","2","0","3"])
        self.assertEqual([r["heavy"] for r in result.requests],[True,False,False,True])
        self.assertEqual(result.requests[3]["output_percentile"],.1)
        self.assertTrue(result.requests[3]["input_heavy"])
        self.assertEqual(result.summary["endpoint_executed_requests"],4)

    def test_classification_is_frozen_and_replay_repeats(self):
        settings = self.settings(batch_size=1,endpoints=(endpoint(concurrency=1),))
        requests = [req(0,1,8),req(1,1,8)]
        first = BaselineRunner(settings,classification_policy=OutputPercentileClassifier(settings,self.reference())).run(requests)
        second = BaselineRunner(settings,classification_policy=OutputPercentileClassifier(settings,self.reference())).run(requests)
        self.assertEqual(first,second)
        self.assertEqual([r["output_percentile_threshold"] for r in first.requests],[.9,.6])
        self.assertEqual([r["heavy"] for r in first.requests],[False,True])
        arrived = [e for e in first.events if e["event"] == "arrived"]
        self.assertTrue(all(e["heavy"] is None and e["classification_pending"] for e in arrived))
        self.assertEqual(first.summary["threshold_values_used"],[.6,.9])

    def test_effective_order_uses_new_classification(self):
        settings = self.settings("percentile_fixed",batch_size=2,batch_order="effective_priority",heavy_priority_discount=.5)
        requests = [req(0,1,9),req(1,1,7)]
        result = BaselineRunner(settings,classification_policy=OutputPercentileClassifier(settings,self.reference())).run(requests)
        self.assertEqual(result.batches[0]["dispatch_order"],["1","0"])
        self.assertEqual([r["effective_priority"] for r in result.requests],[.5,1])

    def test_invalid_modes_thresholds_and_scope(self):
        for changes in ({"output_classification":"bad"},{"output_percentile_threshold":0},
                        {"output_percentile_threshold":1},{"output_percentile_threshold":True},
                        {"output_classification":"percentile_fixed","batch_scope":"heavy_only"}):
            with self.assertRaises(ValueError):
                config(**changes)
