from pprint import pprint
import click
import re
import sys

import github
import yaml

RULE_INCLUDE = 'include'
RULE_IGNORE = 'ignore'

JOB_ACTION_EDIT = 'update'
JOB_ACTION_CREATE = 'create'

@click.command()
@click.option('--conf', default="./config.yaml", help="Configuration file path.")
@click.option('--token-path', default="~/.github-token", help="Github token path.")
@click.option('--dry-run', default=False, is_flag=True, help="Show what you would do, but don't do it.")
def main(conf, token_path, dry_run):
    """The main function"""
    config = read_config(conf)
    token = read_token(token_path)
    g = github.Github(token)

    # Get the leader repo and it's labels
    leader_labels = {}
    leaders = 0
    for repo in config['github']['repositories']:
        if 'leader' in repo and repo['leader'] == True:
            if leaders > 0:
                # TODO: handle error
                pass
            leaders += 1
            print(f"Fetching labels from the leader repository {config['github']['organization']}/{repo['name']}...")
            leader_labels, leader_labels_ignored = read_repo_labels(g, config['github']['organization'], repo['name'], config['rules'])
    
    # Get the other (target) repo's labels
    target_labels = {}
    for repo in config['github']['repositories']:
        if 'leader' not in repo or repo['leader'] == False:
            print(f"Fetching labels from the target repository {config['github']['organization']}/{repo['name']}...")
            target_labels[repo['name']], _ = read_repo_labels(g, config['github']['organization'], repo['name'], config['rules'])
    
    # Collect sync jobs as a list of tuples of (repository name, label name, action)
    jobs = []
    for repo in target_labels.keys():
        print(f'Comparing labels for repository {repo}...')

        for key in leader_labels.keys():
            if key in target_labels[repo]:
                diff = compare_labels(leader_labels[key], target_labels[repo][key])
                if len(diff) > 0:
                    jobs.append((repo, key, JOB_ACTION_EDIT))
            else:
                jobs.append((repo, key, JOB_ACTION_CREATE))

    if len(jobs) == 0:
        print("\nEverything in sync! ☺️")
        sys.exit(0)

    # Print the plan
    print('\nHere is our synchronization plan:\n')
    for job in jobs:
        (repo, label, action) = job
        print(f'- {repo}: {action} label {label}')
    
    print(f"\n{len(leader_labels_ignored.keys())} labels from the leader repository will be ignored.\n")

    if dry_run:
        print("Exiting without actions, as --dry-run was used.")
        sys.exit(0)

    response = confirm('Do you want to continue to synchronize labels as described above?')
    if response == False:
        sys.exit(0)
    
    ### Execute sync

    repo_handlers = {}
    for repo in target_labels.keys():
        repo_handlers[repo] = repo = g.get_repo(f"{config['github']['organization']}/{repo}")
    
    print('\nExecuting synchronization plan')
    for job in jobs:
        (repo, label, action) = job
        print(f'{repo}: {action} label {label}')
        if action == JOB_ACTION_CREATE:
            repo_handlers[repo].create_label(name=leader_labels[label].name, color=leader_labels[label].color, description=leader_labels[label].description)
        elif action == JOB_ACTION_EDIT:
            desc = leader_labels[label].description
            if desc is None or desc == '':
                desc = github.GithubObject.NotSet
            target_labels[repo][label].edit(name=leader_labels[label].name, color=leader_labels[label].color, description=desc)


def read_repo_labels(github_client, organization, repo, filter_rules=None):
    """
    Reads all labels from the given GitHub repo, then filters them
    according to the configured rules. Returns a dict where the
    key is the label string. Returnes two dicts:

    1. The labels to be used
    2. The labels filtered out
    """
    repo = github_client.get_repo(f'{organization}/{repo}')
    labels = repo.get_labels()
    out = {}

    for label in labels:
        out[label.name] = label

    if filter_rules is not None:
        return filter_labels(out, filter_rules)
    else:
        return out, {}


def filter_labels(labels, rules):
    """
    Filters the given dict of labels by the given rules.
    Returnes two dicts:

    1. The labels to be used
    2. The labels filtered out
    """
    out = {RULE_INCLUDE: {}, RULE_IGNORE: {}}

    for key in labels.keys():
        # Iterate rules and look for matches.
        # Last matching rule wins and sets the mode.
        mode = None
        for rule in rules:
            # Sanity check
            if 'regex' not in rule or 'mode' not in rule:
                error(f'invalid rule: {rule}')
            if rule['mode'] not in (RULE_IGNORE, RULE_INCLUDE):
                error(f'invalid rule mode: {rule["mode"]}')
            
            match = rule['regex'].match(key)
            if match is not None:
                mode = rule["mode"]
        
        if mode in (RULE_IGNORE, RULE_INCLUDE):
            out[mode][key] = labels[key]
        elif mode is None:
            out[RULE_IGNORE][key] = labels[key]

    return out[RULE_INCLUDE], out[RULE_IGNORE]


def compare_labels(a, b):
    """
    Compares two GitHub labels (name, description, color) and returns a list
    of fields that are different. Returns empty list if there are no differences.
    """
    diff = []
    if a.name != b.name:
        diff.append('name')
    if a.color != b.color:
        diff.append('color')
    if a.description != b.description:
        diff.append('description')

    return diff


def confirm(question):
    """
    Ask user to enter Y or N (case-insensitive).
    :return: True if the answer is Y.
    :rtype: bool
    """
    answer = ""
    while answer not in ["y", "n"]:
        answer = input(f"{question} [Y/N]? ").lower()
    return answer == "y"


def read_config(path):
    with open(path, "r") as input:
        data = yaml.load(input, Loader=yaml.Loader)
        for n in range(len(data['rules'])):
            data['rules'][n]['regex'] = re.compile(data['rules'][n]['pattern'])
        return data


def read_token(path):
    with open(path, "r") as input:
        token = input.readline()
        return token.strip()


def error(message):
    print(f'ERROR: {message}', file=sys.stderr)
    sys.exit(1)


if __name__ == '__main__':
    main()
