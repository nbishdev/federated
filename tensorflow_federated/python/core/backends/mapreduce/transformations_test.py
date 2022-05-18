# Copyright 2019, The TensorFlow Federated Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from absl.testing import absltest
import tensorflow as tf

from tensorflow_federated.proto.v0 import computation_pb2
from tensorflow_federated.python.core.backends.mapreduce import form_utils
from tensorflow_federated.python.core.backends.mapreduce import mapreduce_test_utils
from tensorflow_federated.python.core.backends.mapreduce import transformations
from tensorflow_federated.python.core.impl.compiler import building_block_factory
from tensorflow_federated.python.core.impl.compiler import building_blocks
from tensorflow_federated.python.core.impl.compiler import intrinsic_defs
from tensorflow_federated.python.core.impl.compiler import transformation_utils
from tensorflow_federated.python.core.impl.compiler import tree_analysis
from tensorflow_federated.python.core.impl.context_stack import set_default_context
from tensorflow_federated.python.core.impl.execution_contexts import sync_execution_context
from tensorflow_federated.python.core.impl.executors import executor_stacks
from tensorflow_federated.python.core.impl.types import computation_types
from tensorflow_federated.python.core.impl.types import placements
from tensorflow_federated.python.core.impl.wrappers import computation_wrapper_instances

DEFAULT_GRAPPLER_CONFIG = tf.compat.v1.ConfigProto()


class CheckExtractionResultTest(absltest.TestCase):

  def get_function_from_first_symbol_binding_in_lambda_result(self, tree):
    """Unwraps a function from a series of nested calls, lambdas and blocks.

    The specific shape being unwrapped here is:

    (_ -> (let (_=_, ...) in _))
                  ^ This is the computation being returned.

    Args:
      tree: A series of nested calls and lambdas as described above.

    Returns:
      Inner function value described above.
    """
    self.assertIsInstance(tree, building_blocks.Lambda)
    self.assertIsNone(tree.parameter_type)
    self.assertIsInstance(tree.result, building_blocks.Block)
    comp_to_return = tree.result.locals[0][1]
    self.assertIsInstance(comp_to_return, building_blocks.Call)
    return comp_to_return.function

  def compiled_computation_for_initialize(self, initialize):
    block = initialize.to_building_block()
    return self.get_function_from_first_symbol_binding_in_lambda_result(block)

  def test_raises_on_none_args(self):
    with self.assertRaisesRegex(TypeError, 'None'):
      transformations.check_extraction_result(
          None, building_blocks.Reference('x', tf.int32))
    with self.assertRaisesRegex(TypeError, 'None'):
      transformations.check_extraction_result(
          building_blocks.Reference('x', tf.int32), None)

  def test_raises_function_and_call(self):
    function = building_blocks.Reference(
        'f', computation_types.FunctionType(tf.int32, tf.int32))
    integer_ref = building_blocks.Reference('x', tf.int32)
    call = building_blocks.Call(function, integer_ref)
    with self.assertRaisesRegex(transformations.MapReduceFormCompilationError,
                                'we have the functional type'):
      transformations.check_extraction_result(function, call)

  def test_raises_non_function_and_compiled_computation(self):
    init = form_utils.get_iterative_process_for_map_reduce_form(
        mapreduce_test_utils.get_temperature_sensor_example()).initialize
    compiled_computation = self.compiled_computation_for_initialize(init)
    integer_ref = building_blocks.Reference('x', tf.int32)
    with self.assertRaisesRegex(transformations.MapReduceFormCompilationError,
                                'we have the non-functional type'):
      transformations.check_extraction_result(integer_ref, compiled_computation)

  def test_raises_function_and_compiled_computation_of_different_type(self):
    init = form_utils.get_iterative_process_for_map_reduce_form(
        mapreduce_test_utils.get_temperature_sensor_example()).initialize
    compiled_computation = self.compiled_computation_for_initialize(init)
    function = building_blocks.Reference(
        'f', computation_types.FunctionType(tf.int32, tf.int32))
    with self.assertRaisesRegex(transformations.MapReduceFormCompilationError,
                                'incorrect TFF type'):
      transformations.check_extraction_result(function, compiled_computation)

  def test_raises_tensor_and_call_to_not_compiled_computation(self):
    function = building_blocks.Reference(
        'f', computation_types.FunctionType(tf.int32, tf.int32))
    ref_to_int = building_blocks.Reference('x', tf.int32)
    called_fn = building_blocks.Call(function, ref_to_int)
    with self.assertRaisesRegex(transformations.MapReduceFormCompilationError,
                                'missing'):
      transformations.check_extraction_result(ref_to_int, called_fn)

  def test_passes_function_and_compiled_computation_of_same_type(self):
    init = form_utils.get_iterative_process_for_map_reduce_form(
        mapreduce_test_utils.get_temperature_sensor_example()).initialize
    compiled_computation = self.compiled_computation_for_initialize(init)
    function = building_blocks.Reference('f',
                                         compiled_computation.type_signature)
    transformations.check_extraction_result(function, compiled_computation)


class ConsolidateAndExtractTest(absltest.TestCase):

  def test_raises_on_none(self):
    with self.assertRaises(TypeError):
      transformations.consolidate_and_extract_local_processing(
          None, DEFAULT_GRAPPLER_CONFIG)

  def test_already_reduced_case(self):
    init = form_utils.get_iterative_process_for_map_reduce_form(
        mapreduce_test_utils.get_temperature_sensor_example()).initialize

    comp = init.to_building_block()

    result = transformations.consolidate_and_extract_local_processing(
        comp, DEFAULT_GRAPPLER_CONFIG)

    self.assertIsInstance(result, building_blocks.CompiledComputation)
    self.assertIsInstance(result.proto, computation_pb2.Computation)
    self.assertEqual(result.proto.WhichOneof('computation'), 'tensorflow')

  def test_reduces_unplaced_lambda_leaving_type_signature_alone(self):
    lam = building_blocks.Lambda('x', tf.int32,
                                 building_blocks.Reference('x', tf.int32))
    extracted_tf = transformations.consolidate_and_extract_local_processing(
        lam, DEFAULT_GRAPPLER_CONFIG)
    self.assertIsInstance(extracted_tf, building_blocks.CompiledComputation)
    self.assertEqual(extracted_tf.type_signature, lam.type_signature)

  def test_reduces_unplaced_lambda_to_equivalent_tf(self):
    lam = building_blocks.Lambda('x', tf.int32,
                                 building_blocks.Reference('x', tf.int32))
    extracted_tf = transformations.consolidate_and_extract_local_processing(
        lam, DEFAULT_GRAPPLER_CONFIG)
    executable_tf = computation_wrapper_instances.building_block_to_computation(
        extracted_tf)
    executable_lam = computation_wrapper_instances.building_block_to_computation(
        lam)
    for k in range(10):
      self.assertEqual(executable_tf(k), executable_lam(k))

  def test_reduces_federated_identity_to_member_identity(self):
    fed_int_type = computation_types.FederatedType(tf.int32, placements.CLIENTS)
    lam = building_blocks.Lambda('x', fed_int_type,
                                 building_blocks.Reference('x', fed_int_type))
    extracted_tf = transformations.consolidate_and_extract_local_processing(
        lam, DEFAULT_GRAPPLER_CONFIG)
    self.assertIsInstance(extracted_tf, building_blocks.CompiledComputation)
    unplaced_function_type = computation_types.FunctionType(
        fed_int_type.member, fed_int_type.member)
    self.assertEqual(extracted_tf.type_signature, unplaced_function_type)

  def test_reduces_federated_map_to_equivalent_function(self):
    lam = building_blocks.Lambda('x', tf.int32,
                                 building_blocks.Reference('x', tf.int32))
    arg_type = computation_types.FederatedType(tf.int32, placements.CLIENTS)
    arg = building_blocks.Reference('arg', arg_type)
    map_block = building_block_factory.create_federated_map_or_apply(lam, arg)
    mapping_fn = building_blocks.Lambda('arg', arg_type, map_block)
    extracted_tf = transformations.consolidate_and_extract_local_processing(
        mapping_fn, DEFAULT_GRAPPLER_CONFIG)
    self.assertIsInstance(extracted_tf, building_blocks.CompiledComputation)
    executable_tf = computation_wrapper_instances.building_block_to_computation(
        extracted_tf)
    executable_lam = computation_wrapper_instances.building_block_to_computation(
        lam)
    for k in range(10):
      self.assertEqual(executable_tf(k), executable_lam(k))

  def test_reduces_federated_apply_to_equivalent_function(self):
    lam = building_blocks.Lambda('x', tf.int32,
                                 building_blocks.Reference('x', tf.int32))
    arg_type = computation_types.FederatedType(tf.int32, placements.CLIENTS)
    arg = building_blocks.Reference('arg', arg_type)
    map_block = building_block_factory.create_federated_map_or_apply(lam, arg)
    mapping_fn = building_blocks.Lambda('arg', arg_type, map_block)
    extracted_tf = transformations.consolidate_and_extract_local_processing(
        mapping_fn, DEFAULT_GRAPPLER_CONFIG)
    self.assertIsInstance(extracted_tf, building_blocks.CompiledComputation)
    executable_tf = computation_wrapper_instances.building_block_to_computation(
        extracted_tf)
    executable_lam = computation_wrapper_instances.building_block_to_computation(
        lam)
    for k in range(10):
      self.assertEqual(executable_tf(k), executable_lam(k))

  def test_reduces_federated_value_at_server_to_equivalent_noarg_function(self):
    zero = building_block_factory.create_tensorflow_constant(
        computation_types.TensorType(tf.int32, shape=[]), 0)
    federated_value = building_block_factory.create_federated_value(
        zero, placements.SERVER)
    federated_value_func = building_blocks.Lambda(None, None, federated_value)
    extracted_tf = transformations.consolidate_and_extract_local_processing(
        federated_value_func, DEFAULT_GRAPPLER_CONFIG)
    executable_tf = computation_wrapper_instances.building_block_to_computation(
        extracted_tf)
    self.assertEqual(executable_tf(), 0)

  def test_reduces_federated_value_at_clients_to_equivalent_noarg_function(
      self):
    zero = building_block_factory.create_tensorflow_constant(
        computation_types.TensorType(tf.int32, shape=[]), 0)
    federated_value = building_block_factory.create_federated_value(
        zero, placements.CLIENTS)
    federated_value_func = building_blocks.Lambda(None, None, federated_value)
    extracted_tf = transformations.consolidate_and_extract_local_processing(
        federated_value_func, DEFAULT_GRAPPLER_CONFIG)
    executable_tf = computation_wrapper_instances.building_block_to_computation(
        extracted_tf)
    self.assertEqual(executable_tf(), 0)

  def test_reduces_lambda_returning_empty_tuple_to_tf(self):
    empty_tuple = building_blocks.Struct([])
    lam = building_blocks.Lambda('x', tf.int32, empty_tuple)
    extracted_tf = transformations.consolidate_and_extract_local_processing(
        lam, DEFAULT_GRAPPLER_CONFIG)
    self.assertIsInstance(extracted_tf, building_blocks.CompiledComputation)


class ConcatenateFunctionOutputsTest(absltest.TestCase):

  def test_raises_on_non_lambda_args(self):
    reference = building_blocks.Reference('x', tf.int32)
    tff_lambda = building_blocks.Lambda('x', tf.int32, reference)
    with self.assertRaises(TypeError):
      transformations.concatenate_function_outputs(tff_lambda, reference)
    with self.assertRaises(TypeError):
      transformations.concatenate_function_outputs(reference, tff_lambda)

  def test_raises_on_non_unique_names(self):
    reference = building_blocks.Reference('x', tf.int32)
    good_lambda = building_blocks.Lambda('x', tf.int32, reference)
    bad_lambda = building_blocks.Lambda('x', tf.int32, good_lambda)
    with self.assertRaises(ValueError):
      transformations.concatenate_function_outputs(good_lambda, bad_lambda)
    with self.assertRaises(ValueError):
      transformations.concatenate_function_outputs(bad_lambda, good_lambda)

  def test_raises_on_different_parameter_types(self):
    int_reference = building_blocks.Reference('x', tf.int32)
    int_lambda = building_blocks.Lambda('x', tf.int32, int_reference)
    float_reference = building_blocks.Reference('x', tf.float32)
    float_lambda = building_blocks.Lambda('x', tf.float32, float_reference)
    with self.assertRaises(TypeError):
      transformations.concatenate_function_outputs(int_lambda, float_lambda)

  def test_parameters_are_mapped_together(self):
    x_reference = building_blocks.Reference('x', tf.int32)
    x_lambda = building_blocks.Lambda('x', tf.int32, x_reference)
    y_reference = building_blocks.Reference('y', tf.int32)
    y_lambda = building_blocks.Lambda('y', tf.int32, y_reference)
    concatenated = transformations.concatenate_function_outputs(
        x_lambda, y_lambda)
    parameter_name = concatenated.parameter_name

    def _raise_on_other_name_reference(comp):
      if isinstance(comp,
                    building_blocks.Reference) and comp.name != parameter_name:
        raise ValueError
      return comp, True

    tree_analysis.check_has_unique_names(concatenated)
    transformation_utils.transform_postorder(concatenated,
                                             _raise_on_other_name_reference)

  def test_concatenates_identities(self):
    x_reference = building_blocks.Reference('x', tf.int32)
    x_lambda = building_blocks.Lambda('x', tf.int32, x_reference)
    y_reference = building_blocks.Reference('y', tf.int32)
    y_lambda = building_blocks.Lambda('y', tf.int32, y_reference)
    concatenated = transformations.concatenate_function_outputs(
        x_lambda, y_lambda)
    self.assertEqual(str(concatenated), '(y -> <y,y>)')


class NormalizedBitTest(absltest.TestCase):

  def test_raises_on_none(self):
    with self.assertRaises(TypeError):
      transformations.normalize_all_equal_bit(None)

  def test_converts_all_equal_at_clients_reference_to_not_equal(self):
    fed_type_all_equal = computation_types.FederatedType(
        tf.int32, placements.CLIENTS, all_equal=True)
    normalized_comp = transformations.normalize_all_equal_bit(
        building_blocks.Reference('x', fed_type_all_equal))
    self.assertEqual(
        normalized_comp.type_signature,
        computation_types.FederatedType(
            tf.int32, placements.CLIENTS, all_equal=False))
    self.assertIsInstance(normalized_comp, building_blocks.Reference)
    self.assertEqual(str(normalized_comp), 'x')

  def test_converts_not_all_equal_at_server_reference_to_equal(self):
    fed_type_not_all_equal = computation_types.FederatedType(
        tf.int32, placements.SERVER, all_equal=False)
    normalized_comp = transformations.normalize_all_equal_bit(
        building_blocks.Reference('x', fed_type_not_all_equal))
    self.assertEqual(
        normalized_comp.type_signature,
        computation_types.FederatedType(
            tf.int32, placements.SERVER, all_equal=True))
    self.assertIsInstance(normalized_comp, building_blocks.Reference)
    self.assertEqual(str(normalized_comp), 'x')

  def test_converts_all_equal_at_clients_lambda_parameter_to_not_equal(self):
    fed_type_all_equal = computation_types.FederatedType(
        tf.int32, placements.CLIENTS, all_equal=True)
    normalized_fed_type = computation_types.FederatedType(
        tf.int32, placements.CLIENTS)
    ref = building_blocks.Reference('x', fed_type_all_equal)
    lam = building_blocks.Lambda('x', fed_type_all_equal, ref)
    normalized_lambda = transformations.normalize_all_equal_bit(lam)
    self.assertEqual(
        lam.type_signature,
        computation_types.FunctionType(fed_type_all_equal, fed_type_all_equal))
    self.assertIsInstance(normalized_lambda, building_blocks.Lambda)
    self.assertEqual(str(normalized_lambda), '(x -> x)')
    self.assertEqual(
        normalized_lambda.type_signature,
        computation_types.FunctionType(normalized_fed_type,
                                       normalized_fed_type))

  def test_converts_not_all_equal_at_server_lambda_parameter_to_equal(self):
    fed_type_not_all_equal = computation_types.FederatedType(
        tf.int32, placements.SERVER, all_equal=False)
    normalized_fed_type = computation_types.FederatedType(
        tf.int32, placements.SERVER)
    ref = building_blocks.Reference('x', fed_type_not_all_equal)
    lam = building_blocks.Lambda('x', fed_type_not_all_equal, ref)
    normalized_lambda = transformations.normalize_all_equal_bit(lam)
    self.assertEqual(
        lam.type_signature,
        computation_types.FunctionType(fed_type_not_all_equal,
                                       fed_type_not_all_equal))
    self.assertIsInstance(normalized_lambda, building_blocks.Lambda)
    self.assertEqual(str(normalized_lambda), '(x -> x)')
    self.assertEqual(
        normalized_lambda.type_signature,
        computation_types.FunctionType(normalized_fed_type,
                                       normalized_fed_type))

  def test_converts_federated_map_all_equal_to_federated_map(self):
    fed_type_all_equal = computation_types.FederatedType(
        tf.int32, placements.CLIENTS, all_equal=True)
    normalized_fed_type = computation_types.FederatedType(
        tf.int32, placements.CLIENTS)
    int_ref = building_blocks.Reference('x', tf.int32)
    int_identity = building_blocks.Lambda('x', tf.int32, int_ref)
    federated_int_ref = building_blocks.Reference('y', fed_type_all_equal)
    called_federated_map_all_equal = building_block_factory.create_federated_map_all_equal(
        int_identity, federated_int_ref)
    normalized_federated_map = transformations.normalize_all_equal_bit(
        called_federated_map_all_equal)
    self.assertEqual(called_federated_map_all_equal.function.uri,
                     intrinsic_defs.FEDERATED_MAP_ALL_EQUAL.uri)
    self.assertIsInstance(normalized_federated_map, building_blocks.Call)
    self.assertIsInstance(normalized_federated_map.function,
                          building_blocks.Intrinsic)
    self.assertEqual(normalized_federated_map.function.uri,
                     intrinsic_defs.FEDERATED_MAP.uri)
    self.assertEqual(normalized_federated_map.type_signature,
                     normalized_fed_type)


if __name__ == '__main__':
  factory = executor_stacks.local_executor_factory()
  context = sync_execution_context.ExecutionContext(executor_fn=factory)
  set_default_context.set_default_context(context)
  absltest.main()
