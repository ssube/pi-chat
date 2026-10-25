"""Include self-contained skill tests in the existing public test-discovery path."""
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tests(loader, tests, pattern):
    for skill, filename in (("environment-doctor", "test_doctor.py"),
                            ("tmux", "test_tmux_control.py")):
        path = ROOT / "skills" / skill / "tests" / filename
        spec = importlib.util.spec_from_file_location("support_" + skill.replace("-", "_"), path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tests.addTests(loader.loadTestsFromModule(module))
    return tests
