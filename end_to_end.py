import ast

import gen_edit_script
import map_asts
import simplify
import muast
import runtime_comparison

student_code = '''
def oneToN(n):
    for n in range (1,n+1):
        n += str(n)
    return n
'''


problem_unit_tests = [
    'oneToN(5) == "12345"',
    'oneToN(1) == "1"',
    'oneToN(10) == "12345678910"',
]

correct_versions = [
    '''
def oneToN(n):
    s = ''
    for digit in range(1,n+1):
        s += str(digit)
    return s
    '''
]

print('Finding fixes for student code:')
print(student_code)
print()
student_tree = muast.MutableAst(ast.parse(student_code))
student_tree.write_dot_file('student_code', 'out/end_to_end_student_ast.dot')

fixed_versions = []
for correct_version in correct_versions:
    print('Comparing to a correct student solution:')
    print(correct_version)
    correct_tree = muast.MutableAst(ast.parse(correct_version))
    index_mapping = map_asts.generate_mapping(student_tree, correct_tree)
    edit_script = gen_edit_script.generate_edit_script(student_tree, correct_tree, index_mapping)
    print('Edit distance to this correct solution:', edit_script.edit_distance)

    print()
    print('Simplifying edit script...')
    simplified_script = simplify.simplify_edit_script(student_tree, problem_unit_tests, edit_script)
    print(f'Removed {edit_script.edit_distance-simplified_script.edit_distance} edits that did not affect correctness')
    print(f'New edit distance: {simplified_script.edit_distance}')

    # generate an AST representing the "fixed" solution by applying the simplified edit script
    fixed_tree = simplified_script.apply(student_tree)
    print('Solution after applying simplified edit script:')
    print(fixed_tree)

    fixed_versions.append((simplified_script.edit_distance, simplified_script, fixed_tree))

best_edit_distance, best_edit_script, best_fixed_version = min(fixed_versions)
print(f'The best(shortest) edit script has {best_edit_distance} edits and results in this corrected code:')
print(best_fixed_version)
print()

print(f'This edit script can be split into {len(best_edit_script.dependent_blocks)} fixes')

print()
print('Performing runtime analysis...')

print('For each unit test, comparing what original student code does vs. the corrected version')
orig_comparisons = []
for unit_test in problem_unit_tests:
    orig_code_comparison = runtime_comparison.RuntimeComparison(student_tree, best_fixed_version, unit_test)
    orig_comparisons.append(orig_code_comparison)
    print(orig_code_comparison)

print('Analyzing the effect of each fix (independent of other fixes) on runtime code performance')
print()
for fix in best_edit_script.dependent_blocks:
    just_the_fix = best_edit_script.filtered_copy(lambda e: e.short_string not in fix)
    partial_solution = just_the_fix.apply(student_tree)
    print('Current fix changes student code to:')
    print(partial_solution.to_compileable_str())
    fix_code_comparisons = [
        runtime_comparison.RuntimeComparison(partial_solution, best_fixed_version, unit_test)
        for unit_test in problem_unit_tests]
    print('This fix, applied to the original code directly, makes the overall runtime performance of the code',
          runtime_comparison.compare_comparisons(orig_comparisons, fix_code_comparisons))
    print('Effect for each unit test:')
    for test, orig, fixed in zip(problem_unit_tests, orig_comparisons, fix_code_comparisons):
        print(test)
        print(orig.describe_improvement_or_regression(fixed))
    print()

