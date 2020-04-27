import importlib
import inspect
import os
import pkgutil
import sys
import warnings
from contextlib import contextmanager
from logging import getLogger
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    List,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
)

from . import _tracing
from .callers import HookResult
from .dist import (
    _top_level_module_to_dist,
    get_metadata,
    importlib_metadata,
    standard_metadata,
)
from .exceptions import (
    PluginError,
    PluginImportError,
    PluginRegistrationError,
    PluginValidationError,
)
from .hooks import HookCaller, HookExecFunc
from .implementation import HookImplementation
from .markers import HookImplementationMarker, HookspecMarker

logger = getLogger(__name__)


class PluginManager:
    """Core class which manages registration of plugin objects and hook calls.

    You can register new hooks by calling :meth:`add_hookspecs(namespace)
    <.PluginManager.add_hookspecs>`. You can register plugin objects (which
    contain hooks) by calling :meth:`register(namespace)
    <.PluginManager.register>`.  The ``PluginManager`` is initialized
    with a ``project_name`` that is used when discovering `hook specifications`
    and `hook implementations`.

    For debugging purposes you may call :meth:`.PluginManager.enable_tracing`
    which will subsequently send debug information to the trace helper.

    Parameters
    ----------
    project_name : str
        The name of the host project.
    discover_entry_point : str, optional
        The default entry_point group to search when discovering plugins with
        :meth:`PluginManager.discover`, by default None
    discover_prefix : str, optional
        The default module prefix to use when discovering plugins with
        :meth:`PluginManager.discover`, by default None

    Examples
    --------

    .. code-block:: python

        from napari-plugin-engine import PluginManager
        import my_hookspecs

        plugin_manager = PluginManager('my_project')
        plugin_manager.add_hookspecs(my_hookspecs)
        plugin_manager.discover(entry_point='app.plugin', prefix='app_')

        # hooks now live plugin_manager.hook
    """

    def __init__(
        self,
        project_name: str,
        *,
        discover_entry_point: str = '',
        discover_prefix: str = '',
    ):
        self.project_name = project_name
        self.discover_entry_point = discover_entry_point
        self.discover_prefix = discover_prefix
        # mapping of name -> Plugin object
        self.plugins: Dict[str, Any] = {}
        # mapping of Plugin object -> HookCaller
        self._plugin2hookcallers: Dict[Any, List[HookCaller]] = {}

        self._blocked: Set[str] = set()

        self.trace = _tracing.TagTracer().get("pluginmanage")
        self.hook = _HookRelay(self)
        self._inner_hookexec: HookExecFunc = lambda c, m, k: c.multicall(
            m, k, firstresult=c.is_firstresult
        )

    @property
    def hooks(self) -> '_HookRelay':
        """An alias for PluginManager.hook"""
        return self.hook

    def _hookexec(
        self,
        caller: HookCaller,
        methods: List[HookImplementation],
        kwargs: dict,
    ) -> HookResult:
        """Returns a function that will call a set of hookipmls with a caller.

        This function will be passed to ``HookCaller`` instances that are
        created during hookspec and plugin registration.

        If :meth:`~.PluginManager.enable_tracing` is used, it will set it's own
        wrapper function at self._inner_hookexec to enable tracing of hook
        calls.

        Parameters
        ----------
        caller : HookCaller
            The HookCaller instance that will call the HookImplementations.
        methods : List[HookImplementation]
            A list of :class:`~napari_plugin_engine.HookImplementation` objects whos functions will
            be called during the hook call loop.
        kwargs : dict
            Keyword arguments to pass when calling the ``HookImplementation``.

        Returns
        -------
        :class:`~napari_plugin_engine.HookResult`
            The result object produced by the multicall loop.
        """
        return self._inner_hookexec(caller, methods, kwargs)

    def discover(
        self,
        path: Optional[str] = None,
        entry_point: str = None,
        prefix: str = None,
        ignore_errors: bool = True,
    ) -> Tuple[int, List[PluginError]]:
        """Discover modules by both naming convention and entry_points

        1) Using naming convention:
            plugins installed in the environment that follow a naming
            convention (e.g. "napari_plugin"), can be discovered using
            `pkgutil`. This also enables easy discovery on pypi

        2) Using package metadata:
            plugins that declare a special key (self.PLUGIN_ENTRYPOINT) in
            their setup.py `entry_points`.  discovered using `pkg_resources`.

        https://packaging.python.org/guides/creating-and-discovering-plugins/

        Parameters
        ----------
        path : str, optional
            If a string is provided, it is added to sys.path before importing,
            and removed at the end. by default True
        entry_point : str, optional
            An entry_point group to search for, by default None
        prefix : str, optional
            If ``provided``, modules in the environment starting with
            ``prefix`` will be imported and searched for hook implementations
            by default None.
        ignore_errors : bool, optional
            If ``True``, errors will be gathered and returned at the end.
            Otherwise, they will be raised immediately. by default True

        Returns
        -------
        (count, errs) : Tuple[int, List[PluginError]]
            The number of succefully loaded modules, and a list of errors that
            occurred (if ``ignore_errors`` was ``True``)
        """
        entry_point = entry_point or self.discover_entry_point
        prefix = prefix or self.discover_prefix

        self.hook._needs_discovery = False
        # allow debugging escape hatch
        if os.environ.get("DISABLE_ALL_PLUGINS"):
            warnings.warn(
                'Plugin discovery disabled due to '
                'environmental variable "DISABLE_ALL_PLUGINS"'
            )
            return 0, []

        errs: List[PluginError] = []
        with temp_path_additions(path):
            count = 0
            count, errs = self.load_entrypoints(entry_point, '', ignore_errors)
            n, err = self.load_modules_by_prefix(prefix, ignore_errors)
            count += n
            errs += err
            if count:
                msg = f'loaded {count} plugins:\n  '
                msg += "\n  ".join([str(p) for p in self.plugins.values()])
                logger.info(msg)

        return count, errs

    @contextmanager
    def discovery_blocked(self) -> Generator:
        """A context manager that temporarily blocks discovery of new plugins.
        """
        current = self.hook._needs_discovery
        self.hook._needs_discovery = False
        try:
            yield
        finally:
            self.hook._needs_discovery = current

    def load_entrypoints(
        self, group: str, name: str = '', ignore_errors=True
    ) -> Tuple[int, List[PluginError]]:
        """Load plugins from distributions with an entry point named ``group``.

        https://packaging.python.org/guides/creating-and-discovering-plugins/#using-package-metadata

        For background on entry points, see the Entry Point specification at
        https://packaging.python.org/specifications/entry-points/

        Parameters
        ----------
        group : str
            The entry_point group name to search for
        name : str, optional
            If provided, loads only plugins named ``name``, by default None.
        ignore_errors : bool, optional
            If ``False``, any errors raised during registration will be
            immediately raised, by default True

        Returns
        -------
        Tuple[int, List[PluginError]]
            A tuple of `(count, errors)` with the number of new modules
            registered and a list of any errors encountered (assuming
            ``ignore_errors`` was ``False``, otherwise they are raised.)

        Raises
        ------
        PluginError
            If ``ignore_errors`` is ``True`` and any errors are raised during
            registration.
        """
        if (not group) or os.environ.get("DISABLE_ENTRYPOINT_PLUGINS"):
            return 0, []
        count = 0
        errors: List[PluginError] = []
        for dist in importlib_metadata.distributions():
            for ep in dist.entry_points:
                if (
                    ep.group != group  # type: ignore
                    or (name and ep.name != name)
                    # already registered
                    or self.is_registered(ep.name)
                    or self.is_blocked(ep.name)
                ):
                    continue

                try:
                    if self._load_and_register(ep, ep.name):
                        count += 1
                except PluginError as e:
                    errors.append(e)
                    self.set_blocked(ep.name)
                    if ignore_errors:
                        continue
                    raise e

        return count, errors

    def load_modules_by_prefix(
        self, prefix: str, ignore_errors: bool = True
    ) -> Tuple[int, List[PluginError]]:
        """Load plugins by module naming convention.

        https://packaging.python.org/guides/creating-and-discovering-plugins/#using-naming-convention

        Parameters
        ----------
        prefix : str
            Any modules found in sys.path whose names begin with ``prefix``
            will be imported and searched for hook implementations.
        ignore_errors : bool, optional
            If ``False``, any errors raised during registration will be
            immediately raised, by default True

        Returns
        -------
        Tuple[int, List[PluginError]]
            A tuple of `(count, errors)` with the number of new modules
            registered and a list of any errors encountered (assuming
            ``ignore_errors`` was ``False``, otherwise they are raised.)

        Raises
        ------
        PluginError
            If ``ignore_errors`` is ``True`` and any errors are raised during
            registration.
        """
        if os.environ.get("DISABLE_PREFIX_PLUGINS") or not prefix:
            return 0, []
        count = 0
        errors: List[PluginError] = []
        for finder, mod_name, ispkg in pkgutil.iter_modules():
            if not mod_name.startswith(prefix):
                continue
            dist = _top_level_module_to_dist().get(mod_name)
            name = dist.metadata.get("name") if dist else mod_name
            if self.is_registered(name) or self.is_blocked(name):
                continue

            try:
                if self._load_and_register(mod_name, name):
                    count += 1
            except PluginError as e:
                errors.append(e)
                self.set_blocked(name)
                if ignore_errors:
                    continue
                raise e

        return count, errors

    def _load_and_register(
        self,
        mod: Union[str, importlib_metadata.EntryPoint],
        plugin_name: Optional[str] = None,
    ) -> Optional[str]:
        """A helper function to register a module or EntryPoint under a name.

        Parameters
        ----------
        mod : str or importlib_metadata.EntryPoint
            The name of a module or an EntryPoint object instance to load.
        plugin_name : str, optional
            Optional name for plugin, by default ``get_canonical_name(plugin)``

        Returns
        -------
        str or None
            canonical plugin name, or ``None`` if the name is blocked from
            registering.

        Raises
        ------
        PluginImportError
            If an exception is raised when importing the module.
        PluginRegistrationError
            If an entry_point is declared that is neither a module nor a class.
        PluginRegistrationError
            If an exception is raised during plugin registration.
        """
        try:
            if isinstance(mod, importlib_metadata.EntryPoint):
                mod_name = mod.value
                module = mod.load()
            else:
                mod_name = mod
                module = importlib.import_module(mod)
            if self.is_registered(module):
                return None
        except Exception as exc:
            raise PluginImportError(
                f'Error while importing module {mod_name}',
                plugin_name=plugin_name,
                cause=exc,
            )
        if not (inspect.isclass(module) or inspect.ismodule(module)):
            raise PluginRegistrationError(
                f'Plugin "{plugin_name}" declared entry_point "{mod_name}"'
                ' which is neither a module nor a class.',
                plugin=module,
                plugin_name=plugin_name,
            )

        try:
            return self.register(module, plugin_name)
        except PluginError:
            raise
        except Exception as exc:
            raise PluginRegistrationError(
                plugin=module, plugin_name=plugin_name, cause=exc
            )

    def register(
        self, namespace: Any, name: Optional[str] = None
    ) -> Optional[str]:
        """Register a plugin and return its canonical name or ``None``.

        Parameters
        ----------
        plugin : Any
            The namespace (class, module, dict, etc...) to register
        name : str, optional
            Optional name for plugin, by default ``get_canonical_name(plugin)``

        Returns
        -------
        str or None
            canonical plugin name, or ``None`` if the name is blocked from
            registering.

        Raises
        ------
        TypeError
            If ``namespace`` is a string.
        ValueError
            if the plugin ``name`` or ``namespace`` is already registered.
        """
        if isinstance(namespace, str):
            raise TypeError("Plugin objects cannot be strings.")

        if isinstance(namespace, dict):
            return self._register_dict(namespace, name)

        plugin_name = name or get_canonical_name(namespace)

        if self.is_blocked(plugin_name):
            return None

        if self.is_registered(plugin_name):
            raise ValueError(f"Plugin name already registered: {plugin_name}")
        if self.is_registered(namespace):
            raise ValueError(f"Plugin module already registered: {namespace}")

        hookcallers = []
        for hookimpl in iter_implementations(namespace, self.project_name):
            hookimpl.plugin_name = plugin_name
            hook_caller = getattr(self.hook, hookimpl.specname, None)
            # if we don't yet have a hookcaller by this name, create one.
            if hook_caller is None:
                hook_caller = HookCaller(hookimpl.specname, self._hookexec)
                setattr(self.hook, hookimpl.specname, hook_caller)
            # otherwise, if it has a specification, validate the new
            # hookimpl against the specification.
            elif hook_caller.has_spec():
                self._verify_hook(hook_caller, hookimpl)
                hook_caller._maybe_apply_history(hookimpl)
            # Finally, add the hookimpl to the hook_caller and the hook
            # caller to the list of callers for this plugin.
            hook_caller._add_hookimpl(hookimpl)
            hookcallers.append(hook_caller)

        self._plugin2hookcallers[namespace] = hookcallers
        self.plugins[plugin_name] = namespace
        return plugin_name

    def _register_dict(
        self, dct: Dict[str, Callable], name: Optional[str] = None, **kwargs
    ) -> Optional[str]:
        """Register a dict as a mapping of method name -> method.

        Parameters
        ----------
        dct : Dict[str, Callable]
            Mapping of method name to method.
        name : Optional[str], optional
            The plugin_name to assign to this object, by default None

        Returns
        -------
        str or None
            canonical plugin name, or ``None`` if the name is blocked from
            registering.
        """
        mark = HookImplementationMarker(self.project_name)
        clean_dct = {
            key: mark(specname=key, **kwargs)(val)
            for key, val in dct.items()
            if inspect.isfunction(val)
        }
        namespace = ensure_namespace(clean_dct)
        return self.register(namespace, name)

    def get_name(self, plugin):
        """ Return name for registered plugin or ``None`` if not registered. """
        for name, val in self.plugins.items():
            if plugin == val:
                return name

    def _ensure_plugin(self, name_or_object: Any) -> Any:
        """Return plugin object given a name or object. Or raise an exception.

        Parameters
        ----------
        name_or_object : Any
            Either a string (in which case it is interpreted as a plugin name),
            or a non-string object (in which case it is assumed to be a plugin
            module or class).

        Returns
        -------
        Any
            The plugin object, if found.

        Raises
        ------
        KeyError
            If the plugin does not exist.
        """
        if isinstance(name_or_object, str):
            plugin_name = name_or_object
        else:
            plugin_name = self.get_name(name_or_object)

        if plugin_name in self.plugins:
            return self.plugins[plugin_name]

        if isinstance(name_or_object, str):
            msg = f"No plugin found with the name {name_or_object}"
        else:
            msg = f"No plugin found with the name {name_or_object}"
        raise KeyError(msg)

    def unregister(self, name_or_object: Any) -> Optional[Any]:
        """Unregister a plugin object or ``plugin_name``.

        Parameters
        ----------
        name_or_object : str or Any
            A module/class object or a plugin name (string).

        Returns
        -------
        module : Any or None
            The module object, or None if the ``name_or_object`` was not found.
        """
        try:
            plugin = self._ensure_plugin(name_or_object)
        except KeyError as e:
            warnings.warn(str(e))
            return None

        del self.plugins[self.get_name(plugin)]

        for hookcaller in self._plugin2hookcallers.pop(plugin, []):
            hookcaller._remove_plugin(plugin)

        return plugin

    def _add_hookspec_dict(self, dct: Dict[str, Callable], **kwargs):
        mark = HookspecMarker(self.project_name)
        clean_dct = {
            key: mark(**kwargs)(val)
            for key, val in dct.items()
            if inspect.isfunction(val)
        }
        namespace = ensure_namespace(clean_dct)
        return self.add_hookspecs(namespace)

    def add_hookspecs(self, namespace: Any):
        """Add new hook specifications defined in the given ``namespace``.

        Functions are recognized if they have been decorated accordingly.
        """
        names = []
        for name in dir(namespace):
            method = getattr(namespace, name)
            if not inspect.isroutine(method):
                continue
            # TODO: make `_spec` a class attribute of HookSpecification
            spec_opts = getattr(method, self.project_name + "_spec", None)
            if spec_opts is not None:
                hook_caller = getattr(self.hook, name, None,)
                if hook_caller is None:
                    hook_caller = HookCaller(
                        name, self._hookexec, namespace, spec_opts,
                    )
                    setattr(
                        self.hook, name, hook_caller,
                    )
                else:
                    # plugins registered this hook without knowing the spec
                    hook_caller.set_specification(
                        namespace, spec_opts,
                    )
                    for hookfunction in hook_caller.get_hookimpls():
                        self._verify_hook(
                            hook_caller, hookfunction,
                        )
                names.append(name)

        if not names:
            raise ValueError(
                f"did not find any {self.project_name!r} hooks in {namespace!r}"
            )

    def is_registered(self, obj: Any) -> bool:
        """Return ``True`` if the plugin is already registered."""
        if isinstance(obj, str):
            return obj in self.plugins
        return obj in self._plugin2hookcallers

    def is_blocked(self, plugin_name: str) -> bool:
        """Return ``True`` if the given plugin name is blocked."""
        return plugin_name in self._blocked

    def set_blocked(self, plugin_name: str, blocked=True):
        """Block registrations of ``plugin_name``, unregister if registered.

        Parameters
        ----------
        plugin_name : str
            A plugin name to block.
        blocked : bool, optional
            Whether to block the plugin.  If ``False`` will "unblock"
            ``plugin_name``.  by default True
        """
        if blocked:
            self._blocked.add(plugin_name)
            if self.is_registered(plugin_name):
                self.unregister(plugin_name)
        else:
            if plugin_name in self._blocked:
                self._blocked.remove(plugin_name)

    # TODO: fix sentinel
    def get_errors(
        self,
        plugin: Optional[Any] = '_NULL',
        error_type: Union[Type[PluginError], str] = '_NULL',
    ) -> List[PluginError]:
        """Return a list of PluginErrors associated with ``plugin``.

        Parameters
        ----------
        plugin : Any
            If provided, will restrict errors to those that were raised by
            ``plugin``.  If a string is provided, it will be interpreted as the
            name of the plugin, otherwise it is assumed to be the actual plugin
            object itself.
        error_type : PluginError
            If provided, will restrict errors to instances of ``error_type``.
        """
        # not using _ensure_plugin because it may not have been successfully
        # registered
        plugin_name = '_NULL'
        if plugin != '_NULL' and isinstance(plugin, str):
            plugin_name = plugin
            plugin = '_NULL'
        return PluginError.get(
            plugin=plugin, plugin_name=plugin_name, error_type=error_type
        )

    def _verify_hook(
        self, hook_caller: HookCaller, hookimpl: HookImplementation
    ):
        """Check validity of a ``hookimpl``

        Parameters
        ----------
        hook_caller : HookCaller
            A :class:`HookCaller` instance.
        hookimpl : HookImplementation
            A :class:`HookImplementation` instance, implementing the hook in
            ``hook_caller``.

        Raises
        ------
        PluginValidationError
            If hook_caller is historic and the hookimpl is a hookwrapper.
        PluginValidationError
            If there are any argument names in the ``hookimpl`` that are not
            in the ``hook_caller.spec``.

        Warns
        -----
        Warning
            If the hookspec has ``warn_on_impl`` flag (usually a deprecation).
        """
        # historic hooks cannot have hookwrappers
        if hook_caller.is_historic() and hookimpl.hookwrapper:
            raise PluginValidationError(
                hookimpl,
                f"Plugin {hookimpl.plugin_name!r}\nhook "
                f"{hook_caller.name!r}\nhistoric incompatible to hookwrapper",
            )

        if not hook_caller.spec:
            return

        # If the hookspec has ``warn_on_impl`` flag show a warning.
        if hook_caller.spec.warn_on_impl:
            warnings.warn_explicit(
                hook_caller.spec.warn_on_impl,
                type(hook_caller.spec.warn_on_impl),
                lineno=hookimpl.function.__code__.co_firstlineno,
                filename=hookimpl.function.__code__.co_filename,
            )

        # If there are any argument names in the hookimpl that are not
        # in the hook specification.
        notinspec = set(hookimpl.argnames) - set(hook_caller.spec.argnames)
        if notinspec:
            raise PluginValidationError(
                hookimpl,
                f"Plugin {hookimpl.plugin_name!r} for hook {hook_caller.name!r}"
                f"\nhookimpl definition: {_formatdef(hookimpl.function)}\n"
                f"Argument(s) {notinspec} are declared in the hookimpl but "
                "can not be found in the hookspec",
            )

    def check_pending(self):
        """Make sure all hooks have a specification, or are optional.

        Raises
        ------
        PluginValidationError
            If a hook implementation that was *not* marked as ``optionalhook``
            has been registered for a non-existent hook specification.
        """
        for name in self.hook.__dict__:
            if name.startswith("_"):
                continue
            hook = getattr(self.hook, name)
            if not hook.has_spec():
                for hookimpl in hook.get_hookimpls():
                    if not hookimpl.optionalhook:
                        raise PluginValidationError(
                            hookimpl,
                            f"unknown hook {name!r} in "
                            f"plugin {hookimpl.plugin!r}",
                        )

    def get_hookcallers(self, plugin: Any) -> Optional[List[HookCaller]]:
        """ get all hook callers for the specified plugin. """
        return self._plugin2hookcallers.get(plugin)

    def add_hookcall_monitoring(
        self,
        before: Callable[[str, List[HookImplementation], dict], None],
        after: Callable[
            [HookResult, str, List[HookImplementation], dict], None
        ],
    ) -> Callable[[], None]:
        """Add before/after tracing functions for all hooks.

        return an undo function which, when called, will remove the added
        tracers.

        ``before(hook_name, hook_impls, kwargs)`` will be called ahead of all
        hook calls and receive a hookcaller instance, a list of HookImplementation
        instances and the keyword arguments for the hook call.

        ``after(outcome, hook_name, hook_impls, kwargs)`` receives the same
        arguments as ``before`` but also a
        :py:class:`napari_plugin_engine.callers._Result` object which
        represents the result of the overall hook call.
        """
        oldcall = self._inner_hookexec

        def traced_hookexec(
            caller: HookCaller, impls: List[HookImplementation], kwargs: dict
        ):
            before(caller.name, impls, kwargs)
            outcome = HookResult.from_call(
                lambda: oldcall(caller, impls, kwargs)
            )
            after(outcome, caller.name, impls, kwargs)
            return outcome

        self._inner_hookexec = traced_hookexec

        def undo():
            self._inner_hookexec = oldcall

        return undo

    def enable_tracing(self):
        """Enable tracing of hook calls and return an undo function. """
        hooktrace = self.trace.root.get("hook")

        def before(hook_name, methods, kwargs):
            hooktrace.root.indent += 1
            hooktrace(hook_name, kwargs)

        def after(
            outcome, hook_name, methods, kwargs,
        ):
            if outcome.excinfo is None:
                hooktrace(
                    "finish", hook_name, "-->", outcome.result,
                )
            hooktrace.root.indent -= 1

        return self.add_hookcall_monitoring(before, after)

    def get_metadata(
        self, plugin: Any, *values
    ) -> Optional[Union[str, Dict[str, Optional[str]]]]:
        """Return metadata values for a given plugin

        Parameters
        ----------
        plugin : Any
            Either a string (in which case it is interpreted as a plugin name),
            or a non-string object (in which case it is assumed to be a plugin
            module or class).
        *values : str
            key(s) to lookup in the plugin object distribution metadata.  At
            least one value must be supplied.

        Raises
        ------
        TypeError
            If no values are supplied.
        KeyError
            If the plugin does not exist.
        """
        if not values:
            raise TypeError(
                'get_metadata() requires at least one positional '
                'argument: the metadata value(s) to lookup'
            )
        # allow other objects to pass through directly to get_metadata
        if isinstance(plugin, str):
            plugin = self._ensure_plugin(plugin)
        return get_metadata(plugin, *values)

    def get_standard_metadata(self, plugin: Any):
        """Return a standard metadata dict for ``plugin``.

        Parameters
        ----------
        plugin : Any
            A plugin name or any object.  If it is a plugin name, it
            *must* be a registered plugin.

        Returns
        -------
        metadata : dict
            A  dicts with plugin metadata. The dict is guaranteed to have the
            following keys: {'plugin_name', 'version', 'summary', 'author',
            'license', 'package', 'email', 'url'}

        Raises
        ------
        KeyError
            If ``plugin`` is a string, but is not a registered plugin_name.
        """
        if isinstance(plugin, str):
            plugin = self._ensure_plugin(plugin)
        plugin_meta = dict(plugin_name=self.get_name(plugin))
        plugin_meta.update(standard_metadata(plugin))
        return plugin_meta

    def list_plugin_metadata(self) -> List[Dict[str, Optional[str]]]:
        """Return list of standard metadata dicts for every registered plugin.

        Returns
        -------
        metadata : list of dict
            A list of dicts with plugin metadata. Every dict in the list is
            guaranteed to have the following keys: {'plugin_name', 'version',
            'summary', 'author', 'license', 'package', 'email', 'url'}
        """
        return [
            self.get_standard_metadata(plugin)
            for plugin in self._plugin2hookcallers
        ]


def _formatdef(func):
    return f"{func.__name__}{str(inspect.signature(func))}"


class _HookRelay:
    """Hook holder object for storing HookCaller instances.

    This object triggers (lazy) discovery of plugins as follows:  When a plugin
    hook is accessed (e.g. plugin_manager.hook.napari_get_reader), if
    ``self._needs_discovery`` is True, then it will trigger autodiscovery on
    the parent plugin_manager. Note that ``PluginManager.__init__`` sets
    ``self.hook._needs_discovery = True`` *after* hook_specifications and
    builtins have been discovered, but before external plugins are loaded.
    """

    def __init__(self, manager: PluginManager):
        self._manager = manager
        self._needs_discovery = True

    def __getattribute__(self, name) -> HookCaller:
        """Trigger manager plugin discovery when accessing hook first time."""
        if name not in ("_needs_discovery", "_manager",):
            if self._needs_discovery:
                self._manager.discover()
        return object.__getattribute__(self, name)

    def items(self) -> List[Tuple[str, HookCaller]]:
        """Iterate through hookcallers, removing private attributes."""
        return [
            (k, val) for k, val in vars(self).items() if not k.startswith("_")
        ]

    def values(self) -> List[HookCaller]:
        """Iterate through hookcallers, removing private attributes."""
        return [val for k, val in vars(self).items() if not k.startswith("_")]


def get_canonical_name(namespace: Any) -> str:
    """ Return canonical name for a plugin object.

    Note that a plugin may be registered under a different name which was
    specified by the caller of :meth:`PluginManager.register(plugin, name)
    <.PluginManager.register>`. To obtain the name of a registered plugin
    use :meth:`get_name(plugin) <.PluginManager.get_name>` instead.
    """
    return getattr(namespace, "__name__", None) or str(id(namespace))


def iter_implementations(
    namespace, project_name: str
) -> Generator[HookImplementation, None, None]:
    # register matching hook implementations of the plugin
    for name in dir(namespace):
        # check all attributes/methods of plugin and look for functions or
        # methods that have a "{self.project_name}_impl" attribute.
        method = getattr(namespace, name)
        if not inspect.isroutine(method):
            continue
        # TODO, make "_impl" a HookImplementation class attribute
        hookimpl_opts = getattr(method, project_name + "_impl", None)
        if not hookimpl_opts:
            continue

        # create the HookImplementation instance for this method
        yield HookImplementation(method, namespace, **hookimpl_opts)


def ensure_namespace(obj: Any, name: str = 'orphan') -> Type:
    """Convert a ``dict`` to an object that provides ``getattr``.

    Parameters
    ----------
    obj : Any
        An object, may be a ``dict``, or a regular namespace object.
    name : str, optional
        A name to use for the new namespace, if created.  by default 'orphan'

    Returns
    -------
    type
        A namespace object. If ``obj`` is a ``dict``, creates a new ``type``
        named ``name``, prepopulated with the key:value pairs from ``obj``.
        Otherwise, if ``obj`` is not a ``dict``, will return the original
        ``obj``.

    Raises
    ------
    ValueError
        If ``obj`` is a ``dict`` that contains keys that are not valid
        `identifiers
        <https://docs.python.org/3.3/reference/lexical_analysis.html#identifiers>`_.
    """
    if isinstance(obj, dict):
        bad_keys = [str(k) for k in obj.keys() if not str(k).isidentifier()]
        if bad_keys:
            raise ValueError(
                f"dict contained invalid identifiers: {', '.join(bad_keys)}"
            )
        return type(name, (), obj)
    return obj


@contextmanager
def temp_path_additions(path: Optional[Union[str, List[str]]]) -> Generator:
    """A context manager that temporarily adds ``path`` to sys.path.

    Parameters
    ----------
    path : str or list of str
        A path or list of paths to add to sys.path

    Yields
    -------
    sys_path : list of str
        The current sys.path for the context.
    """
    if isinstance(path, (str, Path)):
        path = [path]
    path = [os.fspath(p) for p in path] if path else []
    to_add = [p for p in path if p not in sys.path]
    for p in to_add:
        sys.path.insert(0, p)
    try:
        yield sys.path
    finally:
        for p in to_add:
            sys.path.remove(p)
