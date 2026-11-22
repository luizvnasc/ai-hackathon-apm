import importlib.util
import pathlib
import sys

SKILLS = pathlib.Path(__file__).resolve().parents[2] / ".apm" / "skills"


def load(skill, script):
    """Import apm/.apm/skills/<skill>/scripts/<script> as a module, with its own copy of hackathon_shared."""
    for name in [m for m in sys.modules if m.split(".")[0] == "hackathon_shared"]:
        del sys.modules[name]  # else every skill would get the package of the first one loaded
    path = SKILLS / skill / "scripts" / script
    spec = importlib.util.spec_from_file_location(f"{skill.replace('-', '_')}__{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
