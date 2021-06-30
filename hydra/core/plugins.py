# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import importlib
import inspect
import pkgutil
import sys
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from inspect import signature
from textwrap import dedent
from timeit import default_timer as timer
from typing import Any, Dict, List, Optional, Tuple, Type

from omegaconf import DictConfig

from hydra._internal.callbacks import Callbacks
from hydra._internal.sources_registry import SourcesRegistry
from hydra._internal.utils2 import deprecation_warning
from hydra.core.config_loader import ConfigLoader
from hydra.core.singleton import Singleton
from hydra.plugins.completion_plugin import CompletionPlugin
from hydra.plugins.config_source import ConfigSource
from hydra.plugins.launcher import Launcher
from hydra.plugins.plugin import Plugin
from hydra.plugins.search_path_plugin import SearchPathPlugin
from hydra.plugins.sweeper import Sweeper
from hydra.types import HydraContext, TaskFunction
from hydra.utils import instantiate


@dataclass
class ScanStats:
    total_time: float = 0
    total_modules_import_time: float = 0
    modules_import_time: Dict[str, float] = field(default_factory=dict)


class Plugins(metaclass=Singleton):
    @staticmethod
    def instance(*args: Any, **kwargs: Any) -> "Plugins":
        ret = Singleton.instance(Plugins, *args, **kwargs)
        assert isinstance(ret, Plugins)
        return ret

    def __init__(self) -> None:
        self.plugin_type_to_subclass_list: Dict[Type[Plugin], List[Type[Plugin]]] = {}
        self.class_name_to_class: Dict[str, Type[Plugin]] = {}
        self.stats: Optional[ScanStats] = None
        self._initialize()

    def _initialize(self) -> None:
        top_level: List[Any] = []
        core_plugins = importlib.import_module("hydra._internal.core_plugins")
        top_level.append(core_plugins)

        try:
            hydra_plugins = importlib.import_module("hydra_plugins")
            top_level.append(hydra_plugins)
        except ImportError:
            # If no plugins are installed the hydra_plugins package does not exist.
            pass
        self.plugin_type_to_subclass_list, self.stats = self._scan_all_plugins(
            modules=top_level
        )
        self.class_name_to_class = {}
        for plugin_type, plugins in self.plugin_type_to_subclass_list.items():
            for clazz in plugins:
                name = f"{clazz.__module__}.{clazz.__name__}"
                self.class_name_to_class[name] = clazz

        # Register config sources
        for source in self.plugin_type_to_subclass_list[ConfigSource]:
            assert issubclass(source, ConfigSource)
            SourcesRegistry.instance().register(source)

    def _instantiate(self, config: DictConfig) -> Plugin:
        from hydra._internal import utils as internal_utils

        classname = internal_utils._get_cls_name(config, pop=False)
        try:
            if classname is None:
                raise ImportError("class not configured")

            if not self.is_in_toplevel_plugins_module(classname):
                # All plugins must be defined inside the approved top level modules.
                # For plugins outside of hydra-core, the approved module is hydra_plugins.
                raise RuntimeError(
                    f"Invalid plugin '{classname}': not the hydra_plugins package"
                )

            if classname not in self.class_name_to_class.keys():
                raise RuntimeError(f"Unknown plugin class : '{classname}'")
            clazz = self.class_name_to_class[classname]
            plugin = instantiate(config=config, _target_=clazz)
            assert isinstance(plugin, Plugin)

        except ImportError as e:
            raise ImportError(
                f"Could not instantiate plugin {classname} : {str(e)}\n\n\tIS THE PLUGIN INSTALLED?\n\n"
            )

        return plugin

    @staticmethod
    def is_in_toplevel_plugins_module(clazz: str) -> bool:

        return clazz.startswith("hydra_plugins.") or clazz.startswith(
            "hydra._internal.core_plugins."
        )

    @staticmethod
    def _setup_plugin(
        plugin: Any,
        task_function: TaskFunction,
        config: DictConfig,
        config_loader: Optional[ConfigLoader] = None,
        hydra_context: Optional[HydraContext] = None,
    ) -> Any:
        """
        With HydraContext introduced in #1581, we need to set up the plugins in a way
        that's compatible with both Hydra 1.0 and Hydra 1.1 syntax.
        This method should be deleted in the next major release.
        """
        assert isinstance(plugin, Sweeper) or isinstance(plugin, Launcher)
        assert (
            config_loader is not None or hydra_context is not None
        ), "config_loader and hydra_context cannot both be None"

        param_keys = signature(plugin.setup).parameters.keys()

        if "hydra_context" not in param_keys:
            # DEPRECATED: remove in 1.2
            # hydra_context will be required in 1.2
            deprecation_warning(
                message=dedent(
                    """
                    Plugin's setup() signature has changed in Hydra 1.1.
                    Support for the old style will be removed in Hydra 1.2.
                    For more info, check https://github.com/facebookresearch/hydra/pull/1581."""
                ),
            )
            config_loader = (
                config_loader
                if config_loader is not None
                else hydra_context.config_loader  # type: ignore
            )
            plugin.setup(  # type: ignore
                config=config,
                config_loader=config_loader,
                task_function=task_function,
            )
        else:
            if hydra_context is None:
                # hydra_context could be None when an incompatible Sweeper instantiates a compatible Launcher
                assert config_loader is not None
                hydra_context = HydraContext(
                    config_loader=config_loader, callbacks=Callbacks()
                )
            plugin.setup(
                config=config, hydra_context=hydra_context, task_function=task_function
            )
        return plugin

    def instantiate_sweeper(
        self,
        *,
        hydra_context: HydraContext,
        task_function: TaskFunction,
        config: DictConfig,
    ) -> Sweeper:
        Plugins.check_usage(self)
        if config.hydra.sweeper is None:
            raise RuntimeError("Hydra sweeper is not configured")
        sweeper = self._instantiate(config.hydra.sweeper)
        sweeper = self._setup_plugin(
            plugin=sweeper,
            task_function=task_function,
            config=config,
            config_loader=None,
            hydra_context=hydra_context,
        )
        assert isinstance(sweeper, Sweeper)
        return sweeper

    def instantiate_launcher(
        self,
        task_function: TaskFunction,
        config: DictConfig,
        config_loader: Optional[ConfigLoader] = None,
        hydra_context: Optional[HydraContext] = None,
    ) -> Launcher:
        Plugins.check_usage(self)
        if config.hydra.launcher is None:
            raise RuntimeError("Hydra launcher is not configured")

        launcher = self._instantiate(config.hydra.launcher)
        launcher = self._setup_plugin(
            plugin=launcher,
            config=config,
            task_function=task_function,
            config_loader=config_loader,
            hydra_context=hydra_context,
        )
        assert isinstance(launcher, Launcher)
        return launcher

    @staticmethod
    def _scan_all_plugins(
        modules: List[Any],
    ) -> Tuple[Dict[Type[Plugin], List[Type[Plugin]]], ScanStats]:

        stats = ScanStats()
        stats.total_time = timer()

        ret: Dict[Type[Plugin], List[Type[Plugin]]] = defaultdict(list)

        plugin_types: List[Type[Plugin]] = [
            Plugin,
            ConfigSource,
            CompletionPlugin,
            Launcher,
            Sweeper,
            SearchPathPlugin,
        ]
        for mdl in modules:
            for importer, modname, ispkg in pkgutil.walk_packages(
                path=mdl.__path__, prefix=mdl.__name__ + ".", onerror=lambda x: None
            ):
                try:
                    module_name = modname.rsplit(".", 1)[-1]
                    # If module's name starts with "_", do not load the module.
                    # But if the module's name starts with a "__", then load the
                    # module.
                    if module_name.startswith("_") and not module_name.startswith("__"):
                        continue
                    import_time = timer()
                    m = importer.find_module(modname)
                    with warnings.catch_warnings(record=True) as recorded_warnings:
                        loaded_mod = m.load_module(modname)
                    import_time = timer() - import_time
                    if len(recorded_warnings) > 0:
                        sys.stderr.write(
                            f"[Hydra plugins scanner] : warnings from '{modname}'. Please report to plugin author.\n"
                        )
                        for w in recorded_warnings:
                            warnings.showwarning(
                                message=w.message,
                                category=w.category,
                                filename=w.filename,
                                lineno=w.lineno,
                                file=w.file,
                                line=w.line,
                            )

                    stats.total_modules_import_time += import_time

                    assert modname not in stats.modules_import_time
                    stats.modules_import_time[modname] = import_time

                    if loaded_mod is not None:
                        for name, obj in inspect.getmembers(loaded_mod):
                            if (
                                inspect.isclass(obj)
                                and issubclass(obj, Plugin)
                                and not inspect.isabstract(obj)
                            ):
                                for plugin_type in plugin_types:
                                    if issubclass(obj, plugin_type):
                                        ret[plugin_type].append(obj)
                except ImportError as e:
                    warnings.warn(
                        message=f"\n"
                        f"\tError importing '{modname}'.\n"
                        f"\tPlugin is incompatible with this Hydra version or buggy.\n"
                        f"\tRecommended to uninstall or upgrade plugin.\n"
                        f"\t\t{type(e).__name__} : {e}",
                        category=UserWarning,
                    )

        stats.total_time = timer() - stats.total_time
        return ret, stats

    def get_stats(self) -> Optional[ScanStats]:
        return self.stats

    def discover(
        self, plugin_type: Optional[Type[Plugin]] = None
    ) -> List[Type[Plugin]]:
        """
        :param plugin_type: class of plugin to discover, None for all
        :return: a list of plugins implementing the plugin type (or all if plugin type is None)
        """
        Plugins.check_usage(self)
        ret: List[Type[Plugin]] = []
        if plugin_type is None:
            plugin_type = Plugin
        assert issubclass(plugin_type, Plugin)
        if plugin_type not in self.plugin_type_to_subclass_list:
            return []
        for clazz in self.plugin_type_to_subclass_list[plugin_type]:
            ret.append(clazz)

        return ret

    @staticmethod
    def check_usage(self_: Any) -> None:
        if not isinstance(self_, Plugins):
            raise ValueError(
                f"Plugins is now a Singleton. usage: Plugins.instance().{inspect.stack()[1][3]}(...)"
            )
