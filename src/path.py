# This code is responsible to handle coupling between code and paths
import os
from os.path import normpath
import re

PROJECT_ROOT_DIRECTORY = CWD = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")
)
SOURCE_DIRECTORY = os.path.join(PROJECT_ROOT_DIRECTORY, "src")
RESULT_DIRECTORY = os.path.join(PROJECT_ROOT_DIRECTORY, "res")
DATA_DIRECTORY = os.path.join(PROJECT_ROOT_DIRECTORY, "data")
DOCUMENTATION_DIRECTORY = os.path.join(PROJECT_ROOT_DIRECTORY, "doc")


def fix_path(path: str) -> str:
    return normpath(re.sub(r"[\\/]", r"\\" if os.path.sep == "\\" else "/", path))
