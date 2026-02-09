#!/usr/bin/env python

import re
import pathlib
from setuptools import find_packages, setup

VERSION_FILE = "fdk_extension/__init__.py"
with pathlib.Path(VERSION_FILE).open() as version_file:
    match = re.search(r"^__version__ = ['\"]([^'\"]*)['\"]",
                      version_file.read(), re.MULTILINE)

if match:
    version = match.group(1)
else:
    raise RuntimeError(f"Unable to find version string in {VERSION_FILE}.")

def _read_requirements(path):
    try:
        lines = pathlib.Path(path).read_text().splitlines()
    except Exception as exc:
        raise RuntimeError(f"Got error while reading {path}") from exc

    requirements = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        requirements.append(stripped)
    return requirements

# Reading requirements.txt file
install_requires = _read_requirements("requirements/requirements.txt")


# Reading requirements for test
test_requires = _read_requirements("requirements/requirements_test.txt")

    

with open("README.md") as readme_file:
    long_description = readme_file.read()

setup(
    name="fdk_extension",
    version=version,
    description="FDK Extension helper library",
    long_description=long_description,
    long_description_content_type="text/markdown",
    license="MIT",
    author="Fynd Developer",
    author_email="dev@fynd.com",
    url="https://github.com/gofynd/fdk-extension-python",
    project_urls={
        "Source Code": "https://github.com/gofynd/fdk-extension-python",
    },
    packages=find_packages(
        exclude=("examples*", "tests*")
    ),
    install_requires=install_requires,
    extras_require={
        "test": test_requires
    },
    keywords=["FDK extension python", "Extension", "FDK"],
    python_requires=">=3.7, <3.11",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Topic :: Software Development :: Libraries",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Intended Audience :: Developers",
        "Natural Language :: English",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3 :: Only"
    ],
)
