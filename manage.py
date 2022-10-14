#!/usr/bin/env python3

import sys
import os
import json
import subprocess
import argparse
import docker
from autotest_backend import redis_connection, plugin_image

SKELETON_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "schema_skeleton.json")


def _schema_skeleton():
    with open(SKELETON_FILE) as f:
        return json.load(f)


def _print(*args_, **kwargs):
    print("[AUTOTESTER]", *args_, **kwargs)


def parse_args():
    parser = argparse.ArgumentParser()

    subparsers = parser.add_subparsers(dest="manager")

    subparsers.add_parser("tester", help="testers", description="testers")
    subparsers.add_parser("plugin", help="plugins", description="plugins")
    subparsers.add_parser("data", help="data", description="data")

    for name, parser_ in subparsers.choices.items():
        subsubparser = parser_.add_subparsers(dest="action")

        install_parser = subsubparser.add_parser("install", help=f"install {parser_.description}")

        if name == "data":
            install_parser.add_argument("name", help="name of a volume to register as a data volume")
        else:
            install_parser.add_argument("paths", nargs="+")

        remove_parser = subsubparser.add_parser("remove", help=f"remove {parser_.description}")
        remove_parser.add_argument("names", nargs="+")

        subsubparser.add_parser("list", help=f"list {parser_.description}")

        subsubparser.add_parser("clean", help=f"remove {parser_.description} that have been deleted on disk.")

    subparsers.add_parser("install", help="install backend")

    managers = {"install": BackendManager, "tester": TesterManager, "plugin": PluginManager, "data": DataManager}

    args = parser.parse_args()

    if args.manager == "install":
        args.action = "install"

    return managers[args.manager], args


class PluginManager:
    def __init__(self, args):
        self.args = args

    def install(self):
        skeleton = _schema_skeleton()
        docker_client = docker.from_env()
        for path in self.args.paths:
            cli = os.path.join(path, "docker.cli")
            if os.path.isfile(cli):
                proc = subprocess.run([cli, "settings"], capture_output=True, check=False, universal_newlines=True)
                if proc.returncode:
                    _print(
                        f"Plugin settings could not be retrieved from plugin at {path}. Failed with:\n{proc.stderr}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                settings = json.loads(proc.stdout)
                plugin_name = list(settings.keys())[0]
                installed_plugins = skeleton["definitions"]["plugins"]["properties"]
                if plugin_name in installed_plugins:
                    _print(f"A plugin named {plugin_name} is already installed", file=sys.stderr, flush=True)
                    continue
                plugin_image(docker_client, plugin_name, path)
                installed_plugins.update(settings)
                redis_connection().set(f"autotest:plugin:{plugin_name}", path)
        redis_connection().set("autotest:schema", json.dumps(skeleton))

    def remove(self, additional=tuple()):
        skeleton = _schema_skeleton()

        installed_plugins = skeleton["definitions"]["plugins"]["properties"]
        for name in self.args.names + additional:
            redis_connection().delete(f"autotest:plugin:{name}")
            if name in installed_plugins:
                installed_plugins.remove(name)
            try:
                installed_plugins.pop(name)
            except KeyError:
                continue
        redis_connection().set("autotest:schema", json.dumps(skeleton))

    @staticmethod
    def _get_installed():
        for plugin_key in redis_connection().keys("autotest:tuple:*"):
            plugin_name = plugin_key.split(":")[-1]
            path = redis_connection().get(plugin_key)
            yield plugin_name, path

    def list(self):
        for plugin_name, path in self._get_installed():
            print(f"{plugin_name} @ {path}")

    def clean(self):
        to_remove = [plugin_name for plugin_name, path in self._get_installed() if not os.path.isdir(path)]
        _print("Removing the following testers:", *to_remove, sep="\t\n")
        self.remove(additional=to_remove)


class TesterManager:
    def __init__(self, args):
        self.args = args

    def install(self):
        skeleton = _schema_skeleton()
        for path in self.args.paths:
            cli = os.path.join(path, "docker.cli")
            if os.path.isfile(cli):
                proc = subprocess.run([cli, "settings"], capture_output=True, check=False, universal_newlines=True)
                if proc.returncode:
                    _print(
                        f"Tester settings could not be retrieved from tester at {path}. Failed with:\n{proc.stderr}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                settings = json.loads(proc.stdout)
                tester_name = settings["properties"]["tester_type"]["const"]
                installed_testers = skeleton["definitions"]["installed_testers"]["enum"]
                if tester_name in installed_testers:
                    _print(f"A tester named {tester_name} is already installed", file=sys.stderr, flush=True)
                    continue
                installed_testers.append(tester_name)
                skeleton["definitions"]["tester_schemas"]["oneOf"].append(settings)
                redis_connection().set(f"autotest:tester:{tester_name}", path)
        redis_connection().set("autotest:schema", json.dumps(skeleton))

    def remove(self, additional=tuple()):
        skeleton = _schema_skeleton()

        tester_settings = skeleton["definitions"]["tester_schemas"]["oneOf"]
        installed_testers = skeleton["definitions"]["installed_testers"]["enum"]
        for name in self.args.names + additional:
            redis_connection().delete(f"autotest:tester:{name}")
            if name in installed_testers:
                installed_testers.remove(name)
            for i, settings in enumerate(tester_settings):
                if name in settings["properties"]["tester_type"]["enum"]:
                    tester_settings.pop(i)
                    break
        redis_connection().set("autotest:schema", json.dumps(skeleton))

    @staticmethod
    def _get_installed():
        for tester_key in redis_connection().keys("autotest:tester:*"):
            tester_name = tester_key.split(":")[-1]
            path = redis_connection().get(tester_key)
            yield tester_name, path

    def list(self):
        for tester_name, path in self._get_installed():
            print(f"{tester_name} @ {path}")

    def clean(self):
        to_remove = [tester_name for tester_name, path in self._get_installed() if not os.path.isdir(path)]
        _print("Removing the following testers:", *to_remove, sep="\t\n")
        self.remove(additional=to_remove)


class DataManager:
    def __init__(self, args):
        self.args = args

    def install(self):
        skeleton = _schema_skeleton()

        installed_volumes = skeleton["definitions"]["data_volumes"]["items"]["enum"]
        name = self.args.name

        if name in installed_volumes:
            _print(f"A volume named {name} is already installed", file=sys.stderr, flush=True)
            return

        try:
            docker.from_env().volumes.get(name)
        except docker.errors.NotFound:
            _print(f"No volume named {name} exists, please create it and try again", file=sys.stderr, flush=True)
            return

        installed_volumes.append(name)
        redis_connection().sadd("autotest:data_volumes", name)
        redis_connection().set("autotest:schema", json.dumps(skeleton))

    def remove(self, additional=tuple()):
        skeleton = _schema_skeleton()
        installed_volumes = skeleton["definitions"]["data_volumes"]["items"]["enum"]
        for name in self.args.names + additional:
            installed_volumes.remove(name)
            redis_connection().srem("autotest:data_volumes", name)
        redis_connection().set("autotest:schema", json.dumps(skeleton))

    def list(self):
        print(*redis_connection().smembers("autotest:data_volumes"), sep="\n")

    def clean(self):
        all_volumes = {v.name for v in docker.from_env().volumes.list()}
        to_remove = [name for name in redis_connection().smembers("autotest:data_volumes") if name not in all_volumes]
        _print("Removing the following testers:", *to_remove, sep="\t\n")
        self.remove(additional=to_remove)


class BackendManager:
    def install(self):
        _print("checking docker connection")
        docker.from_env().ping()


if __name__ == "__main__":
    MANAGER, ARGS = parse_args()
    getattr(MANAGER(ARGS), ARGS.action)()
