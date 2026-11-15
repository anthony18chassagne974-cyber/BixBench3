"""Shared 6-vCPU experiment launcher. Provider credentials stay in the environment."""
import argparse
import json
import os
from pathlib import Path


def build_command(args):
    config = json.loads(args.config.read_text())
    command = ['uv', 'run', '--locked', 'bixbench3', 'run', '--task', args.task,
               '--project', args.project, '--results-bucket', args.results_bucket,
               '--condition', args.condition, '--run-description', args.description]
    for key in ['machine_type', 'boot_disk_gb', 'boot_disk_type', 'model', 'effort', 'runtime_image', 'tracking_grade_every']:
        value = args.boot_disk_type if key == 'boot_disk_type' and args.boot_disk_type else config[key]
        command.extend(['--' + key.replace('_', '-'), str(value)])
    for key in ['no10x_runtime', 'track_live']:
        if config.get(key):
            command.append('--' + key.replace('_', '-'))
    if args.dry_run:
        command.append('--dry-run')
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parents[1] / 'configs/team-6vcpu.json')
    for name in ['task', 'project', 'results-bucket', 'condition', 'description']:
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--boot-disk-type', choices=['pd-standard', 'pd-balanced', 'pd-ssd'])
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    command = build_command(args)
    os.execvp(command[0], command)


if __name__ == '__main__':
    main()
