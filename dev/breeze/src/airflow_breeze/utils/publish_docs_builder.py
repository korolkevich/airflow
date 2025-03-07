# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import os
import re
import shlex
import shutil
from glob import glob
from pathlib import Path
from subprocess import run

from airflow_breeze.global_constants import get_airflow_version
from airflow_breeze.utils.console import Output, get_console
from airflow_breeze.utils.docs_errors import DocBuildError, parse_sphinx_warnings
from airflow_breeze.utils.helm_chart_utils import chart_version
from airflow_breeze.utils.publish_docs_helpers import load_package_data, pretty_format_path
from airflow_breeze.utils.spelling_checks import SpellingError, parse_spelling_warnings

PROCESS_TIMEOUT = 15 * 60

ROOT_PROJECT_DIR = Path(__file__).parents[5].resolve()
DOCS_DIR = os.path.join(ROOT_PROJECT_DIR, "docs")


class PublishDocsBuilder:
    """Documentation builder for Airflow Docs Publishing."""

    def __init__(self, package_name: str, output: Output | None, verbose: bool):
        self.package_name = package_name
        self.output = output
        self.verbose = verbose

    @property
    def _doctree_dir(self) -> str:
        return f"{DOCS_DIR}/_doctrees/docs/{self.package_name}"

    @property
    def _inventory_cache_dir(self) -> str:
        return f"{DOCS_DIR}/_inventory_cache"

    @property
    def is_versioned(self):
        """Is current documentation package versioned?"""
        # Disable versioning. This documentation does not apply to any released product and we can update
        # it as needed, i.e. with each new package of providers.
        return self.package_name not in ("apache-airflow-providers", "docker-stack")

    @property
    def _build_dir(self) -> str:
        if self.is_versioned:
            version = "stable"
            return f"{DOCS_DIR}/_build/docs/{self.package_name}/{version}"
        else:
            return f"{DOCS_DIR}/_build/docs/{self.package_name}"

    @property
    def log_spelling_filename(self) -> str:
        """Log from spelling job."""
        return os.path.join(self._build_dir, f"output-spelling-{self.package_name}.log")

    @property
    def log_spelling_output_dir(self) -> str:
        """Results from spelling job."""
        return os.path.join(self._build_dir, f"output-spelling-results-{self.package_name}")

    @property
    def log_build_filename(self) -> str:
        """Log from build job."""
        return os.path.join(self._build_dir, f"output-build-{self.package_name}.log")

    @property
    def log_build_warning_filename(self) -> str:
        """Warnings from build job."""
        return os.path.join(self._build_dir, f"warning-build-{self.package_name}.log")

    @property
    def _current_version(self):
        if not self.is_versioned:
            raise Exception("This documentation package is not versioned")
        if self.package_name == "apache-airflow":
            return get_airflow_version()
        if self.package_name.startswith("apache-airflow-providers-"):
            all_providers_yaml = load_package_data(include_suspended=True)
            provider = next(p for p in all_providers_yaml if p["package-name"] == self.package_name)
            return provider["versions"][0]
        if self.package_name == "helm-chart":
            return chart_version()
        return Exception(f"Unsupported package: {self.package_name}")

    @property
    def _publish_dir(self) -> str:
        if self.is_versioned:
            return f"docs-archive/{self.package_name}/{self._current_version}"
        else:
            return f"docs-archive/{self.package_name}"

    @property
    def _src_dir(self) -> str:
        return f"{DOCS_DIR}/{self.package_name}"

    def clean_files(self) -> None:
        """Cleanup all artifacts generated by previous builds."""
        api_dir = os.path.join(self._src_dir, "_api")

        shutil.rmtree(api_dir, ignore_errors=True)
        shutil.rmtree(self._build_dir, ignore_errors=True)
        os.makedirs(api_dir, exist_ok=True)
        os.makedirs(self._build_dir, exist_ok=True)

    def check_spelling(self, verbose: bool) -> list[SpellingError]:
        """
        Checks spelling

        :param verbose: whether to show output while running
        :return: list of errors
        """
        spelling_errors = []
        os.makedirs(self._build_dir, exist_ok=True)
        shutil.rmtree(self.log_spelling_output_dir, ignore_errors=True)
        os.makedirs(self.log_spelling_output_dir, exist_ok=True)

        build_cmd = [
            "sphinx-build",
            "-W",  # turn warnings into errors
            "--color",  # do emit colored output
            "-T",  # show full traceback on exception
            "-b",  # builder to use
            "spelling",
            "-c",
            DOCS_DIR,
            "-d",  # path for the cached environment and doctree files
            self._doctree_dir,
            self._src_dir,  # path to documentation source files
            self.log_spelling_output_dir,
        ]

        env = os.environ.copy()
        env["AIRFLOW_PACKAGE_NAME"] = self.package_name
        if verbose:
            get_console(output=self.output).print(
                f"[info]{self.package_name:60}:[/] Executing cmd: ",
                " ".join(shlex.quote(c) for c in build_cmd),
            )
            get_console(output=self.output).print(
                f"[info]{self.package_name:60}:[/] The output is hidden until an error occurs."
            )
        with open(self.log_spelling_filename, "w") as output:
            completed_proc = run(
                build_cmd,
                cwd=self._src_dir,
                env=env,
                stdout=output if not verbose else None,
                stderr=output if not verbose else None,
                timeout=PROCESS_TIMEOUT,
            )
        if completed_proc.returncode != 0:
            spelling_errors.append(
                SpellingError(
                    file_path=None,
                    line_no=None,
                    spelling=None,
                    suggestion=None,
                    context_line=None,
                    message=(
                        f"Sphinx spellcheck returned non-zero exit status: {completed_proc.returncode}."
                    ),
                )
            )
            warning_text = ""
            for filepath in glob(f"{self.log_spelling_output_dir}/**/*.spelling", recursive=True):
                with open(filepath) as spelling_file:
                    warning_text += spelling_file.read()

            spelling_errors.extend(parse_spelling_warnings(warning_text, self._src_dir))
            get_console(output=self.output).print(
                f"[info]{self.package_name:60}:[/] [red]Finished spell-checking with errors[/]"
            )
        else:
            if spelling_errors:
                get_console(output=self.output).print(
                    f"[info]{self.package_name:60}:[/] [yellow]Finished spell-checking with warnings[/]"
                )
            else:
                get_console(output=self.output).print(
                    f"[info]{self.package_name:60}:[/] [green]Finished spell-checking successfully[/]"
                )
        return spelling_errors

    def build_sphinx_docs(self, verbose: bool) -> list[DocBuildError]:
        """
        Build Sphinx documentation.

        :param verbose: whether to show output while running
        :return: list of errors
        """
        build_errors = []
        os.makedirs(self._build_dir, exist_ok=True)

        build_cmd = [
            "sphinx-build",
            "-T",  # show full traceback on exception
            "--color",  # do emit colored output
            "-b",  # builder to use
            "html",
            "-d",  # path for the cached environment and doctree files
            self._doctree_dir,
            "-c",
            DOCS_DIR,
            "-w",  # write warnings (and errors) to given file
            self.log_build_warning_filename,
            self._src_dir,
            self._build_dir,  # path to output directory
        ]
        env = os.environ.copy()
        env["AIRFLOW_PACKAGE_NAME"] = self.package_name
        if verbose:
            get_console(output=self.output).print(
                f"[info]{self.package_name:60}:[/] Executing cmd: ",
                " ".join(shlex.quote(c) for c in build_cmd),
            )
        else:
            get_console(output=self.output).print(
                f"[info]{self.package_name:60}:[/] Running sphinx. "
                f"The output is hidden until an error occurs."
            )
        with open(self.log_build_filename, "w") as output:
            completed_proc = run(
                build_cmd,
                cwd=self._src_dir,
                env=env,
                stdout=output if not verbose else None,
                stderr=output if not verbose else None,
                timeout=PROCESS_TIMEOUT,
            )
        if completed_proc.returncode != 0:
            build_errors.append(
                DocBuildError(
                    file_path=None,
                    line_no=None,
                    message=f"Sphinx returned non-zero exit status: {completed_proc.returncode}.",
                )
            )
        if os.path.isfile(self.log_build_warning_filename):
            with open(self.log_build_warning_filename) as warning_file:
                warning_text = warning_file.read()
            # Remove 7-bit C1 ANSI escape sequences
            warning_text = re.sub(r"\x1B[@-_][0-?]*[ -/]*[@-~]", "", warning_text)
            build_errors.extend(parse_sphinx_warnings(warning_text, self._src_dir))
        if build_errors:
            get_console(output=self.output).print(
                f"[info]{self.package_name:60}:[/] [red]Finished docs building with errors[/]"
            )
        else:
            get_console(output=self.output).print(
                f"[info]{self.package_name:60}:[/] [green]Finished docs building successfully[/]"
            )
        return build_errors

    def publish(self, override_versioned: bool, airflow_site_dir: str):
        """Copy documentation packages files to airflow-site repository."""
        get_console(output=self.output).print(f"Publishing docs for {self.package_name}")
        output_dir = os.path.join(airflow_site_dir, self._publish_dir)
        pretty_source = pretty_format_path(self._build_dir, os.getcwd())
        pretty_target = pretty_format_path(output_dir, airflow_site_dir)
        get_console(output=self.output).print(f"Copy directory: {pretty_source} => {pretty_target}")
        if os.path.exists(output_dir):
            if self.is_versioned:
                if override_versioned:
                    get_console(output=self.output).print(f"Overriding previously existing {output_dir}! ")
                else:
                    get_console(output=self.output).print(
                        f"Skipping previously existing {output_dir}! "
                        f"Delete it manually if you want to regenerate it!"
                    )
                    get_console(output=self.output).print()
                    return
            shutil.rmtree(output_dir)
        shutil.copytree(self._build_dir, output_dir)
        if self.is_versioned:
            with open(os.path.join(output_dir, "..", "stable.txt"), "w") as stable_file:
                stable_file.write(self._current_version)
        get_console(output=self.output).print()
