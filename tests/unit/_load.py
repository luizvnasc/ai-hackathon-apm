import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SKILLS = ROOT / ".apm" / "skills"


def load(skill, script):
    """Import apm/.apm/skills/<skill>/scripts/<script> as a module, with its own copy of hackathon_shared."""
    for name in [m for m in sys.modules if m.split(".")[0] == "hackathon_shared"]:
        del sys.modules[name]  # else every skill would get the package of the first one loaded
    path = SKILLS / skill / "scripts" / script
    return _import(f"{skill.replace('-', '_')}__{path.stem}", path)


def load_shared(module):
    """Import apm/shared/<module>.py, the source of the copies in the skills."""
    return _import(f"shared__{module}", ROOT / "shared" / f"{module}.py")


def _import(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
