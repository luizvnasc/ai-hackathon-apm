import importlib.util
import pathlib

SKILLS = pathlib.Path(__file__).resolve().parents[2] / ".apm" / "skills"


def load(skill, script):
    """Import apm/.apm/skills/<skill>/scripts/<script> as a module."""
    path = SKILLS / skill / "scripts" / script
    spec = importlib.util.spec_from_file_location(f"{skill.replace('-', '_')}__{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
