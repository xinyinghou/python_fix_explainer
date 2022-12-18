# logic for comparing the execution of two versions of some code (usually partially-buggy code to corrected version)
# and for comparing these comparisons:
# given two partially-buggy versions (e.g. original student code and student code with partial fix)
# and one totally corrected version, all of the same basic code, how does each buggy version compare to the corrected?
# which buggy-to-correct comparison is better/worse in terms of the buggy version being closer/further away
# from the corrected?
import difflib
from enum import Enum
from functools import total_ordering
from typing import List
from dataclasses import dataclass


import muast
import get_runtime_effects
import map_bytecode


# Given a sequence of ops executed at runtime, and an op-to-node_id mapping,
# return an equivalent sequence using node_ids,
# being careful about ops that aren't mapped to any nodes
def get_runtime_node_sequence(
        runtime_op_sequence: List[get_runtime_effects.TracedOp],
        op_to_node: dict,
        default_prefix: str = 'unmapped_op'):

    return \
        [
            (
                op_to_node[op_trace.op_id]
                if op_trace.op_id in op_to_node
                else str((default_prefix, op_trace.op_id))
            )
            for op_trace in runtime_op_sequence
        ]


# Helper class for RuntimeComparison below.
# A dataclass for tracking metadata about a particular executed bytecode op in a sequence resulting from
# an execution trace.
# The metadata describes whether (and how) this op is mapped onto an op from another sequence that is being compared to
# this sequence.
@dataclass
class RuntimeOpMappingData:
    is_mapped: bool = False
    mapped_op_index: int = -1
    value_matches: bool = False


# A class for computing and storing runtime comparison between two versions of some code,
# running against the same unit test.
# in our use case, this is usually a comparison between some buggy version ("source")
# and a fully corrected version("dest").

# This class implements a total ordering, so that we can compare comparisons,
# under the assumption that the "dest" code is the same in both RuntimeComparisons being compared,
# and represents a canonical expected/correct way the code should run.
@total_ordering
class RuntimeComparison:
    def __init__(self, source_tree, dest_tree, test_string):
        # Store basic info
        self.source_tree = source_tree
        self.dest_tree = dest_tree
        self.test_string = test_string

        self.source_index_to_node = {n.index: n for n in muast.breadth_first(source_tree)}
        self.source_code = source_tree.to_compileable_str()
        self.dest_code = dest_tree.to_compileable_str()

        # TODO: compute some of these things (e.g. dest static mappings)
        #  less often than once per unit test run per candidate source_tree?
        # compute and store traces of running unit test for each version
        self.source_trace = get_runtime_effects.run_test(self.source_code, test_string)
        self.dest_trace = get_runtime_effects.run_test(self.dest_code, test_string)

        # record run outcomes of source code for easy access:
        self.run_status = self.source_trace.run_outcome
        self.run_completed = (self.source_trace.run_outcome == 'completed')
        self.test_passed = self.source_trace.eval_result

        self.source_op_to_node = map_bytecode.gen_op_to_node_mapping(source_tree)
        self.dest_op_to_node = map_bytecode.gen_op_to_node_mapping(dest_tree)

        ### Find the longest common subsequence (LCS) between the two runtime traces.

        # for matching the sequences, use ids of AST nodes that correspond to the bytecode ops that were executed
        source_node_trace = get_runtime_node_sequence(
            self.source_trace.ops_list, self.source_op_to_node, default_prefix='source')
        dest_node_trace = get_runtime_node_sequence(
            self.dest_trace.ops_list, self.dest_op_to_node, default_prefix='dest')

        # Note: this logic depends on the assumption that source_tree and dest_tree share the same node ids
        # for nodes that are mapped to each other through the AST mapping between them
        # This assumption is true when dest_tree was generated by applying an edit script to the source_tree.

        # use SequenceMatcher to find the LCS based solely on matching AST nodes.
        runtime_diff = difflib.SequenceMatcher(None, source_node_trace, dest_node_trace, autojunk=False).get_opcodes()

        # Now go through the LCS result to:
        # (1) create explicit maps between indices in the two runtime sequences that were matched in the LCS
        # (2) record where *output values* of running matched nodes matched and where they diverged
        # (2.5) find the last op where these values matched
        #       (this is the "deviation point" - after this, the two runs deviate from each other
        #        and are no longer computing the same thing)
        self.total_match_size = 0
        self.last_matching_val_dest = 0  # a measure of how far we got in the dest script while actually matching values
        self.last_matching_val_source = 0  # same for source script

        # lists of RuntimeOpMappingData that track the mapping metadata between the two traces in both directions
        self.source_runtime_mapping_to_dest = [RuntimeOpMappingData() for _ in range(len(source_node_trace))]
        self.dest_runtime_mapping_to_source = [RuntimeOpMappingData() for _ in range(len(dest_node_trace))]

        for tag, i1, i2, j1, j2 in runtime_diff:
            if tag == 'equal':
                self.total_match_size += (i2 - i1)
                for s_i, d_i in zip(range(i1, i2), range(j1, j2)):
                    # Update metadata to reflect that the ops with these indices in the traace are in fact mapped
                    self.source_runtime_mapping_to_dest[s_i].is_mapped = True
                    self.dest_runtime_mapping_to_source[d_i].is_mapped = True
                    # Record which index maps to which, in both directions
                    self.source_runtime_mapping_to_dest[s_i].mapped_op_index = d_i
                    self.dest_runtime_mapping_to_source[d_i].mapped_op_index = s_i
                    # Find matching values
                    source_vals = self.source_trace.ops_list[s_i].pushed_values
                    dest_vals = self.dest_trace.ops_list[d_i].pushed_values
                    if len(source_vals) > 0 and source_vals == dest_vals:
                        # Record that the values do in fact match
                        self.source_runtime_mapping_to_dest[s_i].value_matches = True
                        self.dest_runtime_mapping_to_source[d_i].value_matches = True
                        # Record the last matching values found so far
                        self.last_matching_val_source = s_i
                        self.last_matching_val_dest = d_i

    # get the python expression corresponding to the op that was traced in self.last_matching_val_source
    def get_last_matching_expression(self):
        correct_runtime_op = self.source_trace.ops_list[self.last_matching_val_source]
        correct_node_id = self.source_op_to_node[correct_runtime_op.op_id]
        node = self.source_index_to_node[correct_node_id]
        return str(node)

    def __str__(self):
        return f'Unit test: {self.test_string}\n' \
               f'test {"finished" if self.run_completed else f"did not finish ({self.run_status})"}\n' \
               f'test {"passed" if self.test_passed else "did not pass"}\n' \
               f'Deviation point (after this op, calculations in the two versions diverge): ' \
               f'{self.last_matching_val_dest} out of {len(self.dest_trace.ops_list)}\n'

    def __lt__(self, other: 'RuntimeComparison'):
        # This RuntimeComparison is "less than" another RuntimeComparison
        # if the source run doesn't get as close to dest run
        # (assumes both dest and unit test was the same in both)
        if other.run_completed and not self.run_completed:
            # our run did not finish, but the other one did finish
            return True
        if self.run_completed and not other.run_completed:
            # our run finished, but the other one did not finish
            return False
        if other.test_passed and not self.test_passed:
            # our run did not pass the unit test, but the other run did
            return True
        if self.test_passed and not other.test_passed:
            # our run passed the unit test, but the other one did not
            return False
        if self.test_passed and other.test_passed:
            # both are passing this test - one is not better than the other, they are equally good
            return False
        # If we are here, then both runs completed but did not pass the unit test.
        # Consider how far each run got compared to dest run:
        # Is the other run's deviation point further along in the dest run than our run's deviation point?
        return self.last_matching_val_dest < other.last_matching_val_dest

    def __eq__(self, other: 'RuntimeComparison'):
        return (other.test_passed and self.test_passed) or \
               (other.run_completed == self.run_completed and
                other.test_passed == self.test_passed and
                other.last_matching_val_dest == self.last_matching_val_dest)

    # Find the first instance in the sequence comparison where the value deviates permanently:
    # The first instance after the deviation point where an executed op is mapped between the source and dest runs,
    # but the value differs.
    # returns tuple of:
    # - index in source trace;
    # - trace data about the source op (inculding the deviating values);
    # - trace data about the dest op (inculding the deviating values)
    def find_first_wrong_value(self):
        for i in range(self.last_matching_val_source, len(self.source_trace)):
            op_trace = self.source_trace.ops_list[i]
            op_trace_mapping = self.source_runtime_mapping_to_dest[i]
            if op_trace_mapping.is_mapped:
                # we found a mapped op trace that's after the last matching value,
                # so we can assume the values don't match
                dest_op_trace = self.dest_trace.ops_list[op_trace_mapping.mapped_op_index]
                if op_trace.pushed_values != dest_op_trace.pushed_values:
                    return i, op_trace, dest_op_trace
        # went through the whole loop and didn't find anything - must not be any mapped ops
        # after the last one where values matched.
        return None, None, None

    def describe_improvement(self, other: 'RuntimeComparison', self_name: str, other_name: str):
        # assuming there *is* an improvement in this RuntimeComparison over other, describe it in words
        if self.run_completed and not other.run_completed:
            return f'The run completed in {self_name}, but did not complete in {other_name} ' \
                   f'({other.run_status}).'
        if self.test_passed and not other.test_passed:
            return f'The test passed in {self_name}, but not in {other_name}.'
        if self.last_matching_val_dest > other.last_matching_val_dest:
            node = self.get_last_matching_expression()
            return f'The following expression evaluated correctly in {self_name}, ' \
                   f'but {other_name} deviated from the expected evaluation path before this expression:' \
                   f' \n {node}'

    # Describe (in human-readable format) whether the effect of running the code
    # got better, worse, or stayed the same from this current version of the comparison to some new version
    def describe_improvement_or_regression(self, new_version: 'RuntimeComparison'):
        if self == new_version:
            return 'The new version of the code performed the same as the old version.'
        elif self < new_version:
            return 'The new version of the code performed better than the old version: \n' + \
                   new_version.describe_improvement(self, 'the new version', 'the old version')
        else:  # self > new_version
            return 'The new version of the code performed worse than the old version: \n' + \
                   self.describe_improvement(new_version, 'the old version', 'the new version')


class Effect(Enum):
    WORSE = 'worse'
    SAME = 'the same'
    MIXED = 'mixed'
    BETTER = 'better'

    def __lt__(self, other):
        member_list = list(self.__class__)
        return member_list.index(self) < member_list.index(other)


# given sets of comparison data of running two versions of code against the same correct version,
# on the same set of unit tests,
# decide whether the new version is a strict improvement over the old version
def compare_comparisons(orig_comps: List[RuntimeComparison], new_comps: List[RuntimeComparison]):
    new_better = 0
    new_worse = 0
    same = 0
    for o_c, n_c in zip(orig_comps, new_comps):
        if o_c < n_c:
            new_better += 1
        elif n_c < o_c:
            new_worse += 1
        else:
            same += 1

    if new_better + new_worse == 0:
        return Effect.SAME
    if new_worse == 0:
        return Effect.BETTER
    if new_better == 0:
        return Effect.WORSE
    return Effect.MIXED
