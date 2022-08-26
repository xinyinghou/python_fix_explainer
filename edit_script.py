# Logic for generating an edit script given a mapping between two ManipulableAst
# The edit script is determined entirely by the mapping,
# though the logic and sequence of deriving it is somewhat nuanced.
import dataclasses
from enum import Enum, auto

import time
from collections import defaultdict

import astor
import networkx as nx

# import const_unroll
import manip_ast
import map_asts
import ast
import copy
from apted import APTED, Config
from difflib import SequenceMatcher


# possible edit actions
class Action(Enum):
    UPDATE = auto()  # Update the value of a node
    INSERT = auto()  # Insert a (leaf) node
    MOVE = auto()    # Move a node (with subtree rooted at that node) elsewhere in the tree
    DELETE = auto()  # Delete a (leaf) node from the tree


# possible edit script stages
# the stages of an edit script always happen in the specific order below.
class Stage(Enum):
    UPDATE = auto()  # Update the values of nodes
    ALIGN_KEYS = auto()  # Move nodes to a different key under the same parent (for keyed child nodes)
    ALIGN = auto()  # Move nodes to a different position under the same parent (for indexed child nodes of a list)
    INSERT = auto()  # Insert (leaf) nodes into tree
    MOVE = auto()  # Move nodes to a different parent
    DELETE = auto()  # Delete (leaf) nodes from the tree


# class describing a particular edit which can be applied to a specific tree (or copy thereof)
@dataclasses.dataclass
class Edit:
    action: Action  # what type of edit action this is
    stage: Stage  # what edit stage it should happen in
    node_id: str  # the id of the node (uuid created when original ManipulableAst was created)
    parent_id: str = None  # the id of the parent node (usually, but not always, in the original tree)

    new_node_id: str = None  # the id of the node that will replace current node (for UPDATE action only)
    # data for moves and inserts into parent with keyed children:
    key_in_parent: str = None  # the key that this node should be under after the edit
    # data for moves and inserts into list parent (with indexed children):
    before: str = None  # the current node should be inserted before this node in its parent
    after: str = None  # the current node should be inserted after this node in its parent

    # is_var_literal_swap: bool = False  # is this edit swapping a literal for a variable? TODO: obsolete?
    is_fix_temp_key: bool = False  # Does this edit fix a node being stored under a temporary key?
    orig_key: str = None  # The original key that the node was under, before being moved to a temporary key
    displaced_by_id: str = None  # id of the node that displaced this node, which created the temporary key

    is_rename: bool = False  # Does this edit rename a variable?
    old_name: str = None  # if this is a variable rename, what was the old name?
    new_name: str = None  # if this is a variable rename, what is the new name?

    # is this edit necessary as part of cleaning up children of a node whose type (and therefore fields) changed?
    is_cleanup_after_node_type_change: bool = False

    def apply_edit(self, index_to_node: dict, nonleaf_OK=False):
        # apply edit described in this object, translating node indices to actual nodes using index_to_node
        # insert/delete of entire subtrees (non-leaves) only allowed if subtree_OK is True.
        node: manip_ast.ManipulableAst = index_to_node[self.node_id]
        if self.action == Action.UPDATE:
            # update: change the contents of node to match some new node
            new_node: manip_ast.ManipulableAst = index_to_node[self.node_id]
            return node.update(new_node)
        elif self.action == Action.DELETE:
            # delete this node from its parent
            if not nonleaf_OK and len(node.children) > 0:
                raise manip_ast.ForbiddenEditException(f'Trying to delete node {node.index} with children')
            return node.parent.remove_child(node)
        elif self.action == Action.INSERT:
            # insert given node; use insert function appropriate for parent node type.
            if not nonleaf_OK and len(node.children) > 0:
                raise manip_ast.ForbiddenEditException(f'Trying to insert node {node.index} with children')
            parent = index_to_node[self.parent_id]
            if self.key_in_parent:
                # inserting at a specific key in the parent
                return parent.add_child_at_key(node, self.key_in_parent)
            else:
                # This edit has no desired key in parent; must be inserting into a list
                before = index_to_node[self.before] if self.before else None
                after = index_to_node[self.after] if self.after else None
                return parent.add_child_between(before, after, node)
        elif self.action == Action.MOVE:
            # align = move = delete+insert
            # deleting/inserting non-leaf subtrees is allowed in this case,
            # since we are actually moving one chunk of code.
            delete_edit = dataclasses.replace(self, action=Action.DELETE)
            delete_edit.apply_edit(index_to_node, nonleaf_OK=True)

            insert_edit = dataclasses.replace(self, action=Action.INSERT)
            return insert_edit.apply_edit(index_to_node, nonleaf_OK=True)


# Generate the edit script - a list of edits (Edit objects) which need to happen in that order to change
# source_tree to exactly match dest_tree, given the already generated index_mapping between them.
# Also generates important metadata for each edit which will be used to:
# - determine dependencies,
# - track variable renaming,
# - probably other dependent logic
def generate_edit_script(source_tree: manip_ast.ManipulableAst,
                         dest_tree: manip_ast.ManipulableAst,
                         index_mapping: set):
    #### Data structures for record keeping ####
    # make deep copies of both trees, because we will be editing them on the fly:
    # TODO: refactor to use Edit.apply_edit for each edit type instead of re-implementing application logic?
    #  (need to be able to make the actual edit at the end, after Edit object is complete.)
    source_tree = copy.deepcopy(source_tree)
    dest_tree = copy.deepcopy(dest_tree)
    index_mapping = copy.deepcopy(index_mapping)

    var_renames_s_to_d = {}  # mappings of variable renames (source -> dest)
    var_renames_d_to_s = {}  # mappings of variable renames (dest -> source)

    source_index_to_node = {}
    for n in manip_ast.breadth_first(source_tree):
        source_index_to_node[n.index] = n

    dest_index_to_node = {}
    for n in manip_ast.breadth_first(dest_tree):
        dest_index_to_node[n.index] = n

    source_to_dest = {}
    dest_to_source = {}
    for s_i, d_i in index_mapping:
        source_to_dest[s_i] = d_i
        dest_to_source[d_i] = s_i

    edit_script = []  # list of edit objects representing edit actions to apply (in that order)
    additional_nodes = {}  # index-to-node map of nodes from the dest tree that are used in the edit script.

    ### helper functions which operate on data above ###
    def is_update_variable_rename(source_node: manip_ast.ManipulableAst, dest_node: manip_ast.ManipulableAst):
        # Is this update operation a rename of a variable?
        # Returns False if this change isn't a variable rename, or data about the rename if it is.
        # TODO: also keep track of properly-mapped parameters/variables and don't try to rename them
        if type(source_node.ast).__name__ == 'Name' and type(dest_node.ast).__name__ == 'Name':
            # the two mapped nodes are variable names
            source_var = source_node.ast.id
            dest_var = dest_node.ast.id
            if type(source_node.ast.ctx).__name__ == 'Store' and type(dest_node.ast.ctx).__name__ == 'Store':
                # both are 'store' operations
                if source_var not in var_renames_s_to_d:
                    # This is a brand-new store operation (not a reassignment) - add to mapping between var renames
                    var_renames_s_to_d[source_var] = dest_var
                    var_renames_d_to_s[dest_var] = source_var
            return True, source_var, dest_var  # yes, this is a variable rename

        elif type(source_node.ast).__name__ == 'arg' and type(dest_node.ast).__name__ == 'arg':
            # these are arguments in some kind of arg list (e.g. parameters to a function)
            source_var = source_node.ast.arg
            dest_var = dest_node.ast.arg
            var_renames_s_to_d[source_var] = dest_var
            var_renames_d_to_s[dest_var] = source_var
            return True, source_var, dest_var  # yes, this is a variable rename

        return False, None, None  # we did not find a reason to consider this a variable rename

    def is_insert_variable_rename(insert_node):
        # Are we inserting a variable that has been renamed?
        if type(insert_node.ast).__name__ == 'Name':
            var_name = insert_node.ast.id
            if var_name in var_renames_d_to_s:
                return True, var_renames_d_to_s[var_name], var_name
        return False, None, None

    #### Enumerate and add edits in stage order: update, align (keys or order), insert, move, delete ####

    # Add edits that belong to update and align stages in depth-first order
    # (which hopefully matches order of execution - this is important to correctly map variable renames)
    # This logic can result in update and align stages being mixed,
    # but this is OK because edits from these stages don't depend on each other.
    for s_n in manip_ast.depth_first(source_tree):
        s_i = s_n.index
        if s_i not in source_to_dest:
            # The stages in this loop operate only on mapped nodes, so we can skip unmapped ones early.
            continue
        d_i = source_to_dest[s_i]
        d_n = dest_index_to_node[d_i]

        ### Update stage ###
        # for each source node that's paired with a dest node, but is different - update the node to use dest node
        if s_n.name != d_n.name:
            edit = Edit(
                action=Action.UPDATE,
                stage=Stage.UPDATE,
                node_id=s_n.index,
                new_node_id=d_n.index
            )

            is_rename, old_name, new_name = is_update_variable_rename(s_n, d_n)
            if is_rename:
                edit.is_rename = is_rename
                edit.old_name = old_name
                edit.new_name = new_name

            edit_script.append(edit)

            # copy the replacement node into additional nodes
            additional_nodes[d_n.index] = manip_ast.ManipulableAst(copy.deepcopy(d_n.ast), node_index=d_n.index,
                                                                   shallow=True)
            # modify source tree to apply edit
            s_n.update(d_n)

        # NOTE: while align-keys examines the mapped node and how it is keyed in its parent,
        # the corresponding align-in-lists stage looks at the mapped node and its children.
        # This is done for simplicity within each stage, but it does make the logic different between them.

        ### Align-keys stage ###
        # for each mapped source node whose parent is not a NodeList,
        # if the key_in_parent is different from the mapped dest, make it the same
        if (s_n.key_in_parent != d_n.key_in_parent) and not s_n.parent.isList \
                and (s_n.parent.index in source_to_dest) and (source_to_dest[s_n.parent.index] == d_n.parent.index):
            # TODO: any reason I'm applying the edit before recording it in this case?
            #  (or more precisely, any reason I'm sometimes doing it before and sometimes after?)
            s_n.parent.remove_child(s_n)
            s_n.parent.add_child_at_key(s_n, d_n.key_in_parent)

            edit_script.append(Edit(
                action=Action.MOVE,
                stage=Stage.ALIGN_KEYS,
                node_id=s_n.index,
                parent_id=s_n.parent.index,
                key_in_parent=d_n.key_in_parent
            ))

        ### Align-in-lists stage ###
        # for each mapped source node that is actually a list,
        # align all children that are mapped to children of the corresponding dest node
        # so that the relative order of the mapped children matches the dest order.
        if s_n.isList:
            # Generate a list of (indices of) the mapped children of s_n, in the order they appear under s_n
            mapped_source_order = []
            for c_s_n in s_n.children:
                # if this child node is mapped to some dest node
                if c_s_n.index in source_to_dest:
                    # and if the parent of the mapped child is also d_n
                    if dest_index_to_node[source_to_dest[c_s_n.index]].parent == d_n:
                        mapped_source_order.append(c_s_n.index)

            # Generate a list of (indices of) the mapped children of s_n, but in the order they appear in dest
            mapped_dest_order = []
            for c_d_n in d_n.children:
                if c_d_n.index in dest_to_source:
                    if source_index_to_node[dest_to_source[c_d_n.index]].parent == s_n:
                        mapped_dest_order.append(dest_to_source[c_d_n.index])

            # Use SequenceMatcher to find longest common sub-sequence of the two sequences:
            align_matches = SequenceMatcher(None, mapped_source_order, mapped_dest_order).get_matching_blocks()
            # get set of nodes that are correctly aligned:
            correctly_aligned = set()
            for m in align_matches:
                correctly_aligned.update(mapped_source_order[m.a:(m.a + m.size)])

            # finally, go through nodes in the "correct" order, and for each one that's not in correctly_aligned,
            # remove it and re-insert it in the right place:
            for i, c_s_i in enumerate(mapped_dest_order):
                if c_s_i not in correctly_aligned:
                    # get the actual nodes in the source tree with which to align c_s_i:
                    before_c = source_index_to_node[mapped_dest_order[i-1]] if i > 0 else None
                    after_c = source_index_to_node[mapped_dest_order[i+1]] if i < len(mapped_dest_order)-1 else None

                    # apply the change to the source tree:
                    move_node = source_index_to_node[c_s_i]
                    remove_index = s_n.remove_child(move_node)
                    add_index = s_n.add_child_between(before_c, after_c, move_node)

                    # append the edit to the edit script:
                    edit_script.append(Edit(
                        action=Action.MOVE,
                        stage=Stage.ALIGN,
                        node_id=move_node.index,
                        parent_id=s_n.index,
                        before=before_c.index if before_c else None,
                        after=after_c.index if after_c else None
                     ))

    ### Helper functions for the rest of the stages ###
    def get_source_parent_of_mapped(dest_index):
        # get the parent of the source node mapped to the dest node at specified index
        source_index = dest_to_source[dest_index]
        source_node = source_index_to_node[source_index]
        return source_node.parent

    def insert_based_on_dest_location(source_node_index, ins_edit: Edit):
        # helper function for insert and move stages (insert node to match where it is in the correct tree)
        # also update the edit object with information on the insert location, if provided
        insert_node = source_index_to_node[source_node_index]
        dest_node = dest_index_to_node[source_to_dest[source_node_index]]
        desired_parent = source_index_to_node[dest_to_source[dest_node.parent.index]]
        ins_edit.parent_id = desired_parent.index

        if desired_parent.isList:
            dest_before, dest_after = dest_node.parent.get_child_neighbors(dest_node)

            # Wait! make sure dest_before and dest_after are actually mapped to some useful source node
            # otherwise it will insert somewhere willy-nilly
            # TODO: unit test?.. (for both 'missing' and 'wrong parent' mapping issues)
            while (dest_before is not None) and (dest_before.index not in dest_to_source or
                                                 get_source_parent_of_mapped(dest_before.index) != desired_parent):
                # keep going backwards until we either reach the end or find a mapped 'before' node
                dest_before, _ = dest_node.parent.get_child_neighbors(dest_before)

            while (dest_after is not None) and (dest_after.index not in dest_to_source or
                                                get_source_parent_of_mapped(dest_after.index) != desired_parent):
                _, dest_after = dest_node.parent.get_child_neighbors(dest_after)

            insert_before = \
                source_index_to_node[dest_to_source[dest_before.index]] \
                if dest_before and dest_before.index in dest_to_source \
                else None
            insert_after = \
                source_index_to_node[dest_to_source[dest_after.index]] \
                    if dest_after and dest_after.index in dest_to_source \
                    else None

            # if insert_before or insert_after are not actually parented under the correct parent,
            # add_child_between will ignore them.
            key = desired_parent.add_child_between(insert_before, insert_after, insert_node)
            ins_edit.before = insert_before.index if insert_before else None
            ins_edit.after = insert_after.index if insert_after else None
            return key

        else:
            # the parent is a non-list node - just insert at same key as dest_node
            desired_key = dest_node.key_in_parent
            if insert_node.key_in_parent and (type(insert_node.key_in_parent) == str):
                if insert_node.key_in_parent.startswith('old_'):
                    # this insert is actually a (necessary) realignment from a temporary key
                    # TODO: doesn't actually mark align-key steps as is_fix_temp_key when they are, e.g. 2/x -> x/2
                    #  (because align-key logic is above,
                    #   need to handle that separately or refactor to use this logic)
                    ins_edit.is_fix_temp_key = True
                    ins_edit.orig_key = insert_node.orig_key
                    ins_edit.displaced_by_id = insert_node.displaced_by
                elif insert_node.key_in_parent not in insert_node.parent.ast._fields:
                    # this is actually a move which cleans up children of nodes whose type has changed
                    ins_edit.is_cleanup_after_node_type_change = True

            desired_parent.add_child_at_key(insert_node, desired_key)

            ins_edit.key_in_parent = desired_key
            return desired_key

    #### Insert step ####
    # assumes all nodes where insertion is happening are aligned correctly
    # also assumes that the roots of the trees map to each other; this is always true in the trees that
    # Python AST produces (the root is a Module node)
    for d_n in manip_ast.breadth_first(dest_tree):
        if d_n.index not in dest_to_source:
            # Create a shallow copy of the node, with the same index.
            to_insert = manip_ast.ManipulableAst(copy.deepcopy(d_n.ast), node_index=d_n.index, shallow=True)
            additional_nodes[to_insert.index] = copy.deepcopy(to_insert)  # make separate copy for actual insertion

            # Update mapping data structures:
            index_mapping.add( (to_insert.index, d_n.index) )  # TODO: obsolete? (and also index_mapping.deepcopy?)
            source_to_dest[to_insert.index] = d_n.index
            dest_to_source[d_n.index] = to_insert.index
            source_index_to_node[to_insert.index] = to_insert

            insert_e = Edit(
                action=Action.INSERT,
                stage=Stage.INSERT,
                node_id=to_insert.index
            )

            is_renamed_var, old_name, new_name = is_insert_variable_rename(to_insert)
            if is_renamed_var:
                insert_e.is_rename = is_renamed_var
                insert_e.old_name = old_name
                insert_e.new_name = new_name

            insert_key = insert_based_on_dest_location(to_insert.index, insert_e)
            edit_script.append(insert_e)

    #### Move step ####
    # assumes each node in correct tree has a match (all insertions have happened)
    for d_n in manip_ast.breadth_first(dest_tree):
        if d_n.parent:  # only non-root nodes (again, roots are assumed to match correctly by now)
            s_i = dest_to_source[d_n.index]
            s_n = source_index_to_node[s_i]
            correct_s_parent = source_index_to_node[dest_to_source[d_n.parent.index]]
            if s_n.parent != correct_s_parent:
                # parent doesn't match - needs to be moved.
                move_e = Edit(
                    action=Action.MOVE,
                    stage=Stage.MOVE,
                    node_id=s_n.index
                )
                s_n.parent.remove_child(s_n)  # remove from old location
                move_key = insert_based_on_dest_location(s_i, move_e)  # insert at new location
                edit_script.append(move_e)

    #### Delete step ####
    # assumes move has happened - any unmatched nodes in source tree do not have any matched children
    # (so can delete unmatched leaves bottom-up)
    actual_postorder = list(manip_ast.postorder(source_tree))  # don't use generator and then edit tree in place...
    for s_n in actual_postorder:
        if s_n.index not in source_to_dest:
            # if node is not mapped to a dest node, delete it
            remove_index = s_n.parent.remove_child(s_n)
            delete_e = Edit(
                action=Action.DELETE,
                stage=Stage.DELETE,
                node_id=s_n.index
            )
            if s_n.key_in_parent and (type(s_n.key_in_parent) == str):
                if s_n.key_in_parent.startswith('old_'):
                    # this is actually a (necessary) deletion of a node with a temporary key
                    delete_e.is_fix_temp_key = True
                    delete_e.orig_key = s_n.orig_key
                    delete_e.displaced_by_id = s_n.displaced_by
                elif s_n.key_in_parent not in s_n.parent.ast._fields:
                    # this is actually a deletion which cleans up children of nodes whose type has changed
                    delete_e.is_cleanup_after_node_type_change = True

            edit_script.append(delete_e)

    ### Verify that edited tree actually matches destination ###
    if str(source_tree) != str(dest_tree):
        print(index_mapping)
        for e in edit_script:
            print(e)
        map_asts.draw_comparison(source_tree, dest_tree, index_mapping, 'except.dot')
        raise Exception(f'Source tree is not equal to dest tree \n{source_tree} \n{dest_tree}')

    return edit_script, additional_nodes, var_renames_s_to_d