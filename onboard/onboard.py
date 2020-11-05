import logging
import sys
from os import getenv
from pathlib import Path
from typing import List

from git import Repo

from ogr import GitlabService
from ogr.abstract import AccessLevel, GitService
from ogr.services.gitlab import GitlabProject
from ogr.services.pagure import PagureService

from add_master_branch import AddMasterBranch
from survey import CentosPkgValidatedConvert

logger = logging.getLogger(__name__)
logging.basicConfig(level=getenv("LOGLEVEL", "INFO"))

DEFAULT_BRANCH = "c8s"
work_dir = Path("/tmp/playground")


class OnboardCentosPKG:
    def __init__(
        self,
        service: GitService,
        namespace: str,
        maintainers: List[str],
        maintainers_group: List[str],
        update: bool,
    ):
        self.service = service
        self.namespace = namespace
        self.maintainers = maintainers
        self.maintainers_group = maintainers_group
        self.update = update

    def create_sg_repo(self, pkg_name):
        logger.info(
            f"Creating source-git repo: {self.namespace}/{pkg_name} at {self.service.instance_url}"
        )
        project = self.service.project_create(
            repo=pkg_name,
            namespace=self.namespace,
            description=f"Source git repo for {pkg_name}.\n"
            f"For more info see: http://packit.dev/docs/source-git/",
        )
        logger.info(f"Project created: {project.get_web_url()}")

        if isinstance(project, GitlabProject):
            project.gitlab_repo.visibility = "public"
            project.gitlab_repo.save()

        for maintainer in self.maintainers:
            project.add_user(maintainer, AccessLevel.maintain)
        for group in self.maintainers_group:
            project.add_group(group, AccessLevel.maintain)

        if isinstance(self.service, PagureService):
            add_master = AddMasterBranch(pkg_name)
            add_master.run()

        return project

    def run(self, pkg_name, branch, skip_build=False):
        action = "Updating" if self.update else "Onboarding"
        logger.info(
            f"{action} {pkg_name} using '{branch}' branch."
            f"{' Skipping build.' if skip_build else ''}"
        )

        project = self.service.get_project(namespace=self.namespace, repo=pkg_name)
        sg_exists = False
        if project.exists():
            logger.info(f"Source repo for {pkg_name} already exists")
            if branch in project.get_branches():
                logger.info(f"Branch {branch} already exists")
                if (
                    isinstance(project, GitlabProject)
                    and project.gitlab_repo.visibility == "private"
                ):
                    logger.info("Making the repository public.")
                    project.gitlab_repo.visibility = "public"
                    project.gitlab_repo.save()
                if not self.update:
                    return
            sg_exists = True
        converter = CentosPkgValidatedConvert(
            package_name=pkg_name, distgit_branch=branch
        )
        converter.run(skip_build=skip_build, clone_sg=sg_exists)
        logger.info(f"converter.result: {converter.result}")
        with open("/in/result.yml", "a+") as out:
            out.write(f"{converter.result}\n")
        if (
            not converter.result
            or "error" in converter.result
            or converter.result.get("conditional_patch")
        ):
            logger.warning(f"{action} aborted for {pkg_name}:")
            return
        logger.info(f"{action} successful for {pkg_name}:")
        if not project.exists():
            self.create_sg_repo(pkg_name)

        git_repo = Repo(converter.src_package_dir)
        git_repo.create_remote("packit", project.get_git_urls()["ssh"])
        # dist2src update moves sg-start tag, we need --force to move it in remote
        git_repo.git.push("packit", branch, tags=True, force=self.update)

        converter.cleanup()


if __name__ == "__main__":

    pagure_token = getenv("PAGURE_TOKEN")
    gitlab_token = getenv("GITLAB_TOKEN")
    update = bool(getenv("UPDATE"))
    if pagure_token:
        ocp = OnboardCentosPKG(
            service=PagureService(
                token=pagure_token, instance_url="https://git.stg.centos.org/"
            ),
            namespace="source-git",
            maintainers=["centosrcm"],
            maintainers_group=["git-packit-team"],
            update=update,
        )
    elif gitlab_token:
        ocp = OnboardCentosPKG(
            service=GitlabService(
                token=gitlab_token, instance_url="https://gitlab.com"
            ),
            namespace="packit-service/src",
            maintainers=[],
            maintainers_group=[],
            update=update,
        )
    else:
        logger.error("Define PAGURE_TOKEN or GITLAB_TOKEN")
        sys.exit(1)

    work_dir.joinpath("rpms").mkdir(parents=True, exist_ok=True)
    work_dir.joinpath("src").mkdir(parents=True, exist_ok=True)

    in_file = "/in/update-pkgs.yml" if update else "/in/input-pkgs.yml"
    with open(in_file, "r") as f:
        in_pkgs = f.readlines()
    for pkg in in_pkgs:
        if not pkg.strip() or pkg.startswith("#"):
            continue

        split = pkg.strip().split(":", maxsplit=1)
        if len(split) == 2:
            package, branch = split
        else:
            package, branch = split[0], DEFAULT_BRANCH
        ocp.run(pkg_name=package, branch=branch, skip_build=bool(getenv("SKIP_BUILD")))
