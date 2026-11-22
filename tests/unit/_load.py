import importlib
import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]
SKILLS = ROOT / ".apm" / "skills"
SHARED_SOURCE = "shared_source"


def load(skill, script):
    """Import apm/.apm/skills/<skill>/scripts/<script> as a module, with its own copy of hackathon_shared."""
    for name in [m for m in sys.modules if m.split(".")[0] == "hackathon_shared"]:
        del sys.modules[name]  # else every skill would get the package of the first one loaded
    path = SKILLS / skill / "scripts" / script
    return _import(f"{skill.replace('-', '_')}__{path.stem}", path)


def load_shared(module):
    """Import apm/shared/<module>.py, the source of the copies in the skills."""
    if SHARED_SOURCE not in sys.modules:
        package = types.ModuleType(SHARED_SOURCE)  # a package, so `from .net import ...` works between the modules
        package.__path__ = [str(ROOT / "shared")]
        sys.modules[SHARED_SOURCE] = package
    return importlib.import_module(f"{SHARED_SOURCE}.{module}")


def _import(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
