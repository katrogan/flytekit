import collections
from abc import abstractmethod
from typing import Any, Dict, Optional, Tuple, Type, Union

from flytekit.annotated.context_manager import (
    BranchEvalMode,
    ExecutionState,
    FlyteContext,
    FlyteEntities,
    RegistrationSettings,
)
from flytekit.annotated.interface import Interface, transform_interface_to_typed_interface
from flytekit.annotated.node import create_and_link_node
from flytekit.annotated.promise import Promise, VoidPromise, create_task_output, translate_inputs_to_literals
from flytekit.annotated.type_engine import TypeEngine
from flytekit.common.exceptions import user as _user_exceptions
from flytekit.common.tasks.task import SdkTask
from flytekit.loggers import logger
from flytekit.models import dynamic_job as _dynamic_job
from flytekit.models import interface as _interface_models
from flytekit.models import literals as _literal_models
from flytekit.models import task as _task_model


def kwtypes(**kwargs) -> Dict[str, Type]:
    """
    Converts the keyword arguments to typed dictionary
    """
    d = collections.OrderedDict()
    for k, v in kwargs.items():
        d[k] = v
    return d


# This is the least abstract task. It will have access to the loaded Python function
# itself if run locally, so it will always be a Python task.
# This is analogous to the current SdkRunnableTask. Need to analyze the benefits of duplicating the class versus
# adding to it. Also thinking that the relationship to SdkTask should be a has-one relationship rather than an is-one.
# I'm not attached to this class at all, it's just here as a stand-in. Everything in this PR is subject to change.
#
# I think the class layers are IDL -> Model class -> SdkBlah class. While the model and generated-IDL classes
# obviously encapsulate the IDL, the SdkTask/Workflow/Launchplan/Node classes should encapsulate the control plane.
# That is, all the control plane interactions we wish to build should belong there. (I think this is how it's done
# already.)
class Task(object):
    def __init__(
        self,
        task_type: str,
        name: str,
        interface: _interface_models.TypedInterface,
        metadata: _task_model.TaskMetadata,
        *args,
        **kwargs,
    ):
        self._task_type = task_type
        self._name = name
        self._interface = interface
        self._metadata = metadata

        # This will get populated only at registration time, when we retrieve the rest of the environment variables like
        # project/domain/version/image and anything else we might need from the environment in the future.
        self._registerable_entity: Optional[SdkTask] = None

        FlyteEntities.entities.append(self)

    @property
    def interface(self) -> _interface_models.TypedInterface:
        return self._interface

    @property
    def metadata(self) -> _task_model.TaskMetadata:
        return self._metadata

    @property
    def name(self) -> str:
        return self._name

    @property
    def task_type(self) -> str:
        return self._task_type

    def get_type_for_input_var(self, k: str, v: Any) -> type:
        """
        Returns the python native type for the given input variable
        # TODO we could use literal type to determine this
        """
        return type(v)

    def get_type_for_output_var(self, k: str, v: Any) -> type:
        """
        Returns the python native type for the given output variable
        # TODO we could use literal type to determine this
        """
        return type(v)

    def get_input_types(self) -> Dict[str, type]:
        """
        Returns python native types for inputs. In case this is not a python native task (base class) and hence
        returns a None. we could deduce the type from literal types, but that is not a required excercise
        # TODO we could use literal type to determine this
        """
        return None

    def _local_execute(self, ctx: FlyteContext, **kwargs) -> Union[Tuple[Promise], Promise, VoidPromise]:
        """
        This code is used only in the case when we want to dispatch_execute with outputs from a previous node
        For regular execution, dispatch_execute is invoked directly.
        """
        # Unwrap the kwargs values. After this, we essentially have a LiteralMap
        # The reason why we need to do this is because the inputs during local execute can be of 2 types
        #  - Promises or native constants
        #  Promises as essentially inputs from previous task executions
        #  native constants are just bound to this specific task (default values for a task input)
        #  Also alongwith promises and constants, there could be dictionary or list of promises or constants
        kwargs = translate_inputs_to_literals(
            ctx, input_kwargs=kwargs, interface=self.interface, native_input_types=self.get_input_types()
        )
        input_literal_map = _literal_models.LiteralMap(literals=kwargs)

        outputs_literal_map = self.dispatch_execute(ctx, input_literal_map)
        if isinstance(outputs_literal_map, VoidPromise):
            return outputs_literal_map
        outputs_literals = outputs_literal_map.literals

        # TODO maybe this is the part that should be done for local execution, we pass the outputs to some special
        #    location, otherwise we dont really need to right? The higher level execute could just handle literalMap
        # After running, we again have to wrap the outputs, if any, back into Promise objects
        output_names = list(self.interface.outputs.keys())
        if len(output_names) != len(outputs_literals):
            # Length check, clean up exception
            raise AssertionError(f"Length difference {len(output_names)} {len(outputs_literals)}")

        vals = [Promise(var, outputs_literals[var]) for var in output_names]
        return create_task_output(vals)

    def __call__(self, *args, **kwargs):
        # When a Task is () aka __called__, there are three things we may do:
        #  a. Task Execution Mode - just run the Python function as Python normally would. Flyte steps completely
        #     out of the way.
        #  b. Compilation Mode - this happens when the function is called as part of a workflow (potentially
        #     dynamic task?). Instead of running the user function, produce promise objects and create a node.
        #  c. Workflow Execution Mode - when a workflow is being run locally. Even though workflows are functions
        #     and everything should be able to be passed through naturally, we'll want to wrap output values of the
        #     function into objects, so that potential .with_cpu or other ancillary functions can be attached to do
        #     nothing. Subsequent tasks will have to know how to unwrap these. If by chance a non-Flyte task uses a
        #     task output as an input, things probably will fail pretty obviously.
        if len(args) > 0:
            raise _user_exceptions.FlyteAssertion(
                f"In Flyte workflows, on keyword args are supported to pass inputs to workflows and tasks."
                f"Aborting execution as detected {len(args)} positional args {args}"
            )

        ctx = FlyteContext.current_context()
        if ctx.compilation_state is not None and ctx.compilation_state.mode == 1:
            return self.compile(ctx, *args, **kwargs)
        elif (
            ctx.execution_state is not None and ctx.execution_state.mode == ExecutionState.Mode.LOCAL_WORKFLOW_EXECUTION
        ):
            if ctx.execution_state.branch_eval_mode == BranchEvalMode.BRANCH_SKIPPED:
                return
            return self._local_execute(ctx, **kwargs)
        else:
            logger.warning("task run without context - executing raw function")
            return self.execute(**kwargs)

    def compile(self, ctx: FlyteContext, *args, **kwargs):
        raise Exception("not implemented")

    def get_task_structure(self) -> SdkTask:
        settings = FlyteContext.current_context().registration_settings
        tk = SdkTask(
            type=self.task_type,
            metadata=self.metadata,
            interface=self.interface,
            custom=self.get_custom(settings),
            container=self.get_container(settings),
        )
        # Reset just to make sure it's what we give it
        tk.id._project = settings.project
        tk.id._domain = settings.domain
        tk.id._name = self.name
        tk.id._version = settings.version
        return tk

    def get_container(self, settings: RegistrationSettings) -> _task_model.Container:
        return None

    def get_custom(self, settings: RegistrationSettings) -> Dict[str, Any]:
        return None

    @abstractmethod
    def dispatch_execute(
        self, ctx: FlyteContext, input_literal_map: _literal_models.LiteralMap,
    ) -> _literal_models.LiteralMap:
        """
        This method translates Flyte's Type system based input values and invokes the actual call to the executor
        This method is also invoked during runtime.
        """
        pass

    @abstractmethod
    def execute(self, **kwargs) -> Any:
        pass


class PythonTask(Task):
    def __init__(
        self, task_type: str, name: str, interface: Interface, metadata: _task_model.TaskMetadata, *args, **kwargs
    ):
        super().__init__(task_type, name, transform_interface_to_typed_interface(interface), metadata)
        self._python_interface = interface

    # TODO lets call this interface and the other as flyte_interface?
    @property
    def python_interface(self):
        return self._python_interface

    def get_type_for_input_var(self, k: str, v: Any) -> type:
        return self._python_interface.inputs[k]

    def get_type_for_output_var(self, k: str, v: Any) -> type:
        return self._python_interface.outputs[k]

    def get_input_types(self) -> Dict[str, type]:
        return self._python_interface.inputs

    def compile(self, ctx: FlyteContext, *args, **kwargs):
        return create_and_link_node(
            ctx,
            entity=self,
            interface=self.python_interface,
            timeout=self.metadata.timeout,
            retry_strategy=self.metadata.retries,
            **kwargs,
        )

    def dispatch_execute(
        self, ctx: FlyteContext, input_literal_map: _literal_models.LiteralMap
    ) -> Union[VoidPromise, _literal_models.LiteralMap, _dynamic_job.DynamicJobSpec]:
        """
        This method translates Flyte's Type system based input values and invokes the actual call to the executor
        This method is also invoked during runtime.
            `VoidPromise` is returned in the case when the task itself declares no outputs.
            `Literal Map` is returned when the task returns either one more outputs in the declaration. Individual outputs
                           may be none
            `DynamicJobSpec` is returned when a dynamic workflow is executed
        """

        # TODO We could support default values here too - but not part of the plan right now
        # Translate the input literals to Python native
        native_inputs = TypeEngine.literal_map_to_kwargs(ctx, input_literal_map, self.python_interface.inputs)

        # TODO: Logger should auto inject the current context information to indicate if the task is running within
        #   a workflow or a subworkflow etc
        logger.info(f"Invoking {self.name} with inputs: {native_inputs}")
        try:
            native_outputs = self.execute(**native_inputs)
        except Exception as e:
            logger.exception(f"Exception when executing {e}")
            raise e
        logger.info(f"Task executed successfully in user level, outputs: {native_outputs}")

        # Short circuit the translation to literal map because what's returned may be a dj spec (or an
        # already-constructed LiteralMap if the dynamic task was a no-op), not python native values
        if isinstance(native_outputs, _literal_models.LiteralMap) or isinstance(
            native_outputs, _dynamic_job.DynamicJobSpec
        ):
            return native_outputs

        expected_output_names = list(self.interface.outputs.keys())
        if len(expected_output_names) == 1:
            native_outputs_as_map = {expected_output_names[0]: native_outputs}
        elif len(expected_output_names) == 0:
            return VoidPromise(self.name)
        else:
            # Question: How do you know you're going to enumerate them in the correct order? Even if autonamed, will
            # output2 come before output100 if there's a hundred outputs? We don't! We'll have to circle back to
            # the Python task instance and inspect annotations again. Or we change the Python model representation
            # of the interface to be an ordered dict and we fill it in correctly to begin with.
            native_outputs_as_map = {expected_output_names[i]: native_outputs[i] for i, _ in enumerate(native_outputs)}

        # We manually construct a LiteralMap here because task inputs and outputs actually violate the assumption
        # built into the IDL that all the values of a literal map are of the same type.
        literals = {}
        for k, v in native_outputs_as_map.items():
            literal_type = self.interface.outputs[k].type
            py_type = self.get_type_for_output_var(k, v)
            if isinstance(v, tuple):
                raise AssertionError(f"Output({k}) in task{self.name} received a tuple {v}, instead of {py_type}")
            literals[k] = TypeEngine.to_literal(ctx, v, py_type, literal_type)
        outputs_literal_map = _literal_models.LiteralMap(literals=literals)
        return outputs_literal_map

    @abstractmethod
    def execute(self, **kwargs) -> Any:
        pass

    def get_registerable_entity(self) -> SdkTask:
        if self._registerable_entity is not None:
            return self._registerable_entity
        self._registerable_entity = self.get_task_structure()
        return self._registerable_entity
