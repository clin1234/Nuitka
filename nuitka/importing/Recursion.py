#     Copyright 2023, Kay Hayen, mailto:kay.hayen@gmail.com
#
#     Part of "Nuitka", an optimizing Python compiler that is compatible and
#     integrates with CPython, but also works on its own.
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.
#
""" Recursion into other modules.

"""

import glob
import os

from nuitka import ModuleRegistry, Options
from nuitka.Errors import NuitkaForbiddenImportEncounter
from nuitka.importing import ImportCache, Importing, StandardLibrary
from nuitka.ModuleRegistry import addUsedModule, getRootTopModule
from nuitka.pgo.PGO import decideInclusionFromPGO
from nuitka.plugins.Plugins import Plugins
from nuitka.PythonVersions import python_version
from nuitka.Tracing import recursion_logger
from nuitka.utils.FileOperations import listDir
from nuitka.utils.Importing import getSharedLibrarySuffixes
from nuitka.utils.ModuleNames import ModuleName


def _recurseTo(module_name, module_filename, module_kind):
    from nuitka.tree import Building

    module, is_added = Building.buildModule(
        module_filename=module_filename,
        module_name=module_name,
        source_code=None,
        is_top=False,
        is_main=False,
        is_extension=module_kind == "extension",
        is_fake=False,
        hide_syntax_error=True,
    )

    ImportCache.addImportedModule(module)

    return module, is_added


def recurseTo(
    signal_change,
    module_name,
    module_filename,
    module_kind,
    using_module_name,
    source_ref,
    reason,
):
    try:
        module = ImportCache.getImportedModuleByNameAndPath(
            module_name, module_filename
        )
    except KeyError:
        module = None

    if module is None:
        Plugins.onModuleRecursion(
            module_filename=module_filename,
            module_name=module_name,
            module_kind=module_kind,
            using_module_name=using_module_name,
            source_ref=source_ref,
        )

        module, added_flag = _recurseTo(
            module_name=module_name,
            module_filename=module_filename,
            module_kind=module_kind,
        )

        if added_flag and signal_change is not None:
            signal_change("new_code", module.getSourceReference(), reason)

    return module


_recursion_decision_cache = {}


def decideRecursion(module_filename, module_name, module_kind, extra_recursion=False):
    key = module_filename, module_name, module_kind, extra_recursion

    if key not in _recursion_decision_cache:
        _recursion_decision_cache[key] = _decideRecursion(
            module_filename, module_name, module_kind, extra_recursion
        )

    return _recursion_decision_cache[key]


def _decideRecursion(module_filename, module_name, module_kind, extra_recursion):
    # Many branches, which make decisions immediately, by returning
    # pylint: disable=too-many-branches,too-many-return-statements
    if module_name == "__main__":
        return False, "Main program is not followed to a second time."

    # In -m mode, when including the package, do not duplicate main program.
    if (
        Options.hasPythonFlagPackageMode()
        and not Options.shallMakeModule()
        and module_name.getBasename() == "__main__"
    ):
        if module_name.getPackageName() == getRootTopModule().getRuntimePackageValue():
            return False, "Main program is already included in package mode."

    plugin_decision = Plugins.onModuleEncounter(
        module_filename=module_filename,
        module_name=module_name,
        module_kind=module_kind,
    )

    if plugin_decision is not None:
        return plugin_decision

    if module_kind == "extension":
        if Options.isStandaloneMode():
            return True, "Extension module needed for standalone mode."
        else:
            return False, "Extension module cannot be inspected."

    # PGO decisions are not overruling plugins, but all command line options, they are
    # supposed to be applied already.
    is_stdlib = StandardLibrary.isStandardLibraryPath(module_filename)

    if not is_stdlib or Options.shallFollowStandardLibrary():
        # TODO: Bad placement of this function or should PGO also know about
        # bytecode modules loaded or not.
        from nuitka.tree.Building import decideCompilationMode

        if (
            decideCompilationMode(is_top=False, module_name=module_name, for_pgo=True)
            == "compiled"
        ):
            pgo_decision = decideInclusionFromPGO(
                module_name=module_name,
                module_kind=module_kind,
            )

            if pgo_decision is not None:
                return pgo_decision, "PGO based decision"

    no_case, reason = module_name.matchesToShellPatterns(
        patterns=Options.getShallFollowInNoCase()
    )

    if no_case:
        return (False, "Module %s instructed by user to not follow to." % reason)

    any_case, reason = module_name.matchesToShellPatterns(
        patterns=Options.getShallFollowModules()
    )

    if any_case:
        return (True, "Module %s instructed by user to follow to." % reason)

    if extra_recursion:
        return (True, "Lives in plug-in directory.")

    if is_stdlib and Options.shallFollowStandardLibrary():
        return (True, "Instructed by user to follow to standard library.")

    if Options.shallFollowAllImports():
        if is_stdlib:
            if StandardLibrary.isStandardLibraryNoAutoInclusionModule(module_name):
                return (
                    True,
                    "Instructed by user to follow all modules, including non-automatic standard library modules.",
                )
        else:
            return (
                True,
                "Instructed by user to follow to all non-standard library modules.",
            )

    if Options.shallFollowNoImports():
        return (None, "Instructed by user to not follow at all.")

    # Means, we were not given instructions how to handle things.
    return (
        None,
        "Default behavior in non-standalone mode, not following without request.",
    )


def isSameModulePath(path1, path2):
    if os.path.basename(path1) == "__init__.py":
        path1 = os.path.dirname(path1)
    if os.path.basename(path2) == "__init__.py":
        path2 = os.path.dirname(path2)

    return os.path.abspath(path1) == os.path.abspath(path2)


def _addIncludedModule(module):
    # Many branches, for the decision is very complex, pylint: disable=too-many-branches

    if Options.isShowInclusion():
        recursion_logger.info(
            "Included '%s' as '%s'."
            % (
                module.getFullName(),
                module,
            )
        )

    ImportCache.addImportedModule(module)

    if module.isCompiledPythonPackage() or module.isUncompiledPythonPackage():
        package_filename = module.getFilename()

        if os.path.isdir(package_filename):
            # Must be a namespace package.
            assert python_version >= 0x300

            package_dir = package_filename

            # Only include it, if it contains actual modules, which will
            # recurse to this one and find it again.
        else:
            package_dir = os.path.dirname(package_filename)

            # Real packages will always be included.
            ModuleRegistry.addRootModule(module)

        if Options.isShowInclusion():
            recursion_logger.info("Package directory '%s'." % package_dir)

        for sub_path, sub_filename in listDir(package_dir):
            if sub_filename in ("__init__.py", "__pycache__"):
                continue

            if Importing.isPackageDir(sub_path) and not os.path.exists(
                sub_path + ".py"
            ):
                checkPluginSinglePath(sub_path, module_package=module.getFullName())
            elif sub_path.endswith(".py"):
                checkPluginSinglePath(sub_path, module_package=module.getFullName())

    elif module.isCompiledPythonModule() or module.isUncompiledPythonModule():
        ModuleRegistry.addRootModule(module)
    elif module.isPythonExtensionModule():
        if Options.isStandaloneMode():
            ModuleRegistry.addRootModule(module)
    else:
        assert False, module


def checkPluginSinglePath(plugin_filename, module_package):
    # The importing wants these to be unique.
    plugin_filename = os.path.abspath(plugin_filename)

    if Options.isShowInclusion():
        recursion_logger.info(
            "Checking detail plug-in path '%s' '%s':"
            % (plugin_filename, module_package)
        )

    module_name, module_kind = Importing.getModuleNameAndKindFromFilename(
        plugin_filename
    )

    module_name = ModuleName.makeModuleNameInPackage(module_name, module_package)

    if module_kind == "extension" and not Options.isStandaloneMode():
        recursion_logger.warning(
            """\
Cannot include extension module '%s' unless using at least standalone mode, \
where they would be copied. In this mode, extension modules are not part of \
the compiled result, and therefore asking to include them makes no sense.
"""
            % module_name.asString()
        )

    if module_kind is not None:
        decision, reason = decideRecursion(
            module_filename=plugin_filename,
            module_name=module_name,
            module_kind=module_kind,
            extra_recursion=True,
        )

        if decision:
            module = recurseTo(
                signal_change=None,
                module_filename=plugin_filename,
                module_name=module_name,
                module_kind=module_kind,
                using_module_name=None,
                source_ref=None,
                reason=reason,
            )

            if module:
                _addIncludedModule(module)
            else:
                recursion_logger.warning(
                    "Failed to include module from '%s'." % plugin_filename
                )
        else:
            recursion_logger.warning(
                "Not allowed to include module '%s' due to '%s'."
                % (module_name, reason)
            )


def checkPluginPath(plugin_filename, module_package):
    if Options.isShowInclusion():
        recursion_logger.info(
            "Checking top level inclusion path '%s' '%s'."
            % (plugin_filename, module_package)
        )

    # Files and package directories are handled here.
    if os.path.isfile(plugin_filename) or Importing.isPackageDir(plugin_filename):
        checkPluginSinglePath(plugin_filename, module_package=module_package)
    # This effectively only covers files known to not be packages due to name
    # or older Python version.
    elif os.path.isdir(plugin_filename):
        for sub_path, sub_filename in listDir(plugin_filename):
            assert sub_filename != "__init__.py"

            if Importing.isPackageDir(sub_path) or sub_path.endswith(".py"):
                checkPluginSinglePath(sub_path, module_package=None)
                continue

            for suffix in getSharedLibrarySuffixes():
                if sub_path.endswith(suffix):
                    checkPluginSinglePath(sub_path, module_package=None)

    else:
        recursion_logger.warning(
            "Failed to include module from '%s'." % plugin_filename
        )


def checkPluginFilenamePattern(pattern):
    if Options.isShowInclusion():
        recursion_logger.info("Checking plug-in pattern '%s':" % pattern)

    assert not os.path.isdir(pattern), pattern

    found = False

    for filename in glob.iglob(pattern):
        if filename.endswith(".pyc"):
            continue

        if not os.path.isfile(filename):
            continue

        found = True
        checkPluginSinglePath(filename, module_package=None)

    if not found:
        recursion_logger.warning(
            "Didn't match any files against pattern '%s'." % pattern
        )


def considerUsedModules(module, signal_change):
    for used_module in module.getUsedModules():
        if used_module.finding == "not-found":
            Importing.warnAbout(
                importing=module,
                source_ref=used_module.source_ref,
                module_name=used_module.module_name,
                level=used_module.level,
            )

        # Nothing was found here
        if used_module.filename is None:
            continue

        try:
            decision, reason = decideRecursion(
                module_filename=used_module.filename,
                module_name=used_module.module_name,
                module_kind=used_module.module_kind,
            )

            if decision:
                new_module = recurseTo(
                    signal_change=signal_change,
                    module_name=used_module.module_name,
                    module_filename=used_module.filename,
                    module_kind=used_module.module_kind,
                    source_ref=used_module.source_ref,
                    using_module_name=module.module_name,
                    reason=reason,
                )

                addUsedModule(
                    module=new_module,
                    using_module=module,
                    usage_tag="import",
                    reason=reason,
                    source_ref=used_module.source_ref,
                )
        except NuitkaForbiddenImportEncounter as e:
            recursion_logger.sysexit(
                "Error, forbidden import of '%s' (intending to avoid '%s') in module '%s' at '%s' encountered."
                % (
                    e.args[0],
                    e.args[1],
                    module.getFullName(),
                    used_module.source_ref.getAsString(),
                )
            )

    try:
        Plugins.considerImplicitImports(module=module, signal_change=signal_change)
    except NuitkaForbiddenImportEncounter as e:
        recursion_logger.sysexit(
            "Error, forbidden import of '%s' (intending to avoid '%s') done implicitly by module '%s'."
            % (e.args[0], e.args[1], module.getFullName())
        )
