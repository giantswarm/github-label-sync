import click
import os
import re
import sys

import github
import yaml

TOKEN_ENV_VAR = 'GITHUB_TOKEN'
DEFAULT_TOKEN_PATH = '~/.github-token'

RULE_INCLUDE = 'include'
RULE_IGNORE = 'ignore'

JOB_ACTION_EDIT = 'update'
JOB_ACTION_CREATE = 'create'

# Pointing to https://github.com/giantswarm/giantswarm/blob/master/data/customers.yaml
CUSTOMER_LIST_REPO = 'giantswarm/giantswarm'
CUSTOMER_LIST_PATH = 'data/customers.yaml'
CUSTOMER_LIST_REF = 'main'  # Replace with branch name or ref to use an alternative version.

class RepoArchivedException(Exception):
    pass

@click.command()
@click.option('--conf', default="./config.yaml", help="Configuration file path.")
@click.option('--token-path', default=None, help=f"Github token path (default: {DEFAULT_TOKEN_PATH}, unless the {TOKEN_ENV_VAR} env var is set).")
@click.option('--dry-run', default=False, is_flag=True, help="Show what you would do, but don't do it.")
@click.option('--yes', default=False, is_flag=True, help="Apply the plan without interactive confirmation (for unattended/CI runs).")
def main(conf, token_path, dry_run, yes):
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

    # Repositories that could not be read are skipped, so one missing or
    # inaccessible repository does not block the sync for all the others.
    # A repository that provably does not exist is a data problem in the
    # repository list, not a sync problem: it is reported as a warning and
    # does not make the run fail. Everything else (a 404 that may also mean
    # "no access", a 403, a 5xx, ...) still makes the run end red, see the
    # exit checks below.
    read_failures = 0
    installation_repos = InstallationRepos(g)

    for repo in config['github']['repositories']:
        if 'leader' not in repo or repo['leader'] == False:
            print(f"Fetching labels from the target repository {config['github']['organization']}/{repo['name']}...")
            try:
                target_labels[repo['name']], _ = read_repo_labels(g, config['github']['organization'], repo['name'], config['rules'])
            except github.GithubException as e:
                read_failures += report_read_error(installation_repos, config['github']['organization'], repo['name'], e)
    
    customer_repos = get_customer_repos(g)
    for cr in customer_repos:
        try:
            print(f"Fetching labels from the customer repository {cr['organization']}/{cr['repository']}...")
            target_labels[cr['repository']], _ = read_repo_labels(g, cr['organization'], cr['repository'], config['rules'])
        except RepoArchivedException:
            print(f"Repo {cr['repository']} has been archived. Skipping.")
        except github.GithubException as e:
            read_failures += report_read_error(installation_repos, cr['organization'], cr['repository'], e)

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
        exit_after_read(read_failures)

    # Print the plan
    print('\nHere is our synchronization plan:\n')
    for job in jobs:
        (repo, label, action) = job
        print(f'- {repo}: {action} label {label}')
    
    print(f"\n{len(leader_labels_ignored.keys())} labels from the leader repository will be ignored.\n")

    if dry_run:
        print("Exiting without actions, as --dry-run was used.")
        exit_after_read(read_failures)

    if yes:
        print("Proceeding without confirmation, as --yes was used.")
    elif confirm('Do you want to continue to synchronize labels as described above?') == False:
        sys.exit(0)
    
    ### Execute sync

    repo_handlers = {}
    for repo in target_labels.keys():
        repo_handlers[repo] = repo = g.get_repo(f"{config['github']['organization']}/{repo}")
    
    print('\nExecuting synchronization plan')
    failures = 0
    for job in jobs:
        (repo, label, action) = job
        print(f'{repo}: {action} label {label}')
        try:
            if action == JOB_ACTION_CREATE:
                repo_handlers[repo].create_label(name=leader_labels[label].name, color=leader_labels[label].color, description=leader_labels[label].description)
            elif action == JOB_ACTION_EDIT:
                desc = leader_labels[label].description
                if desc is None or desc == '':
                    desc = github.GithubObject.NotSet
                target_labels[repo][label].edit(name=leader_labels[label].name, color=leader_labels[label].color, description=desc)
        except github.GithubException as e:
            # Log and carry on, so one broken label does not block the rest of the plan.
            print(f'ERROR: {e}')
            failures += 1

    if failures > 0:
        # Still end the run red: an unattended run must not look green when labels
        # were not applied, otherwise the Slack alert for the schedule never fires.
        error(f'{failures} of {len(jobs)} label operations failed.')

    exit_after_read(read_failures)


def report_read_error(installation_repos, organization, reponame, exception):
    """
    Reports a repository that could not be read. Returns the number of read
    failures to count (0 or 1).

    A 404 is only a warning when the token can prove that the repository does
    not exist. GitHub also answers 404 for a repository the token has no access
    to, and that case must make the run fail like any other error.
    """
    if isinstance(exception, github.UnknownObjectException) and installation_repos.is_gone(organization, reponame):
        warning(f"{organization}/{reponame}: repository does not exist. Skipping.")
        return 0

    print(f"ERROR: {organization}/{reponame}: {exception}")
    return 1


class InstallationRepos:
    """
    The repositories an App installation token can see, fetched once on first
    use from GET /installation/repositories. Used to tell a repository that is
    gone from one the token has no access to.
    """

    def __init__(self, github_client):
        self._client = github_client
        self._loaded = False
        self._selection = None
        self._names = set()
        self._owners = set()

    def is_gone(self, organization, reponame):
        """
        True only if the installation has access to all repositories of the
        given organization and this repository is not among them. Any doubt
        (not an installation token, selected repositories only, another
        organization, no repositories at all) returns False.
        """
        self._load()
        if self._selection != 'all':
            return False
        if organization.lower() not in self._owners:
            return False
        return f'{organization}/{reponame}'.lower() not in self._names

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        page = 1
        try:
            while True:
                _, data = self._client.requester.requestJsonAndCheck(
                    'GET', '/installation/repositories', parameters={'per_page': 100, 'page': page})
                self._selection = data.get('repository_selection')
                repositories = data.get('repositories', [])
                for r in repositories:
                    self._names.add(r['full_name'].lower())
                    self._owners.add(r['owner']['login'].lower())
                # Walk until an empty page. total_count is only an early exit, so a
                # missing or wrong count can never leave the list incomplete (an
                # incomplete list would wrongly declare repositories gone).
                if not repositories:
                    break
                total = data.get('total_count')
                if isinstance(total, int) and total > 0 and len(self._names) >= total:
                    break
                page += 1
        except github.GithubException:
            # Not an installation token (e.g. a personal token for local use): we
            # cannot tell a missing repository from a missing permission.
            self._selection = None


def exit_after_read(read_failures):
    """
    Ends the run. Exits 1 if any repository could not be read, so that an
    unattended run never looks green while repositories were skipped.
    """
    if read_failures > 0:
        error(f'{read_failures} repositories could not be read and were skipped.')
    sys.exit(0)


def read_repo_labels(github_client, organization, reponame, filter_rules=None):
    """
    Reads all labels from the given GitHub repo, then filters them
    according to the configured rules. Returns a dict where the
    key is the label string. Returnes two dicts:

    1. The labels to be used
    2. The labels filtered out
    """
    repo = github_client.get_repo(f'{organization}/{reponame}')
    if repo.archived:
        raise RepoArchivedException()

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


def get_customer_repos(github_client):
    repo = github_client.get_repo(CUSTOMER_LIST_REPO)
    file = repo.get_contents(path=CUSTOMER_LIST_PATH, ref=CUSTOMER_LIST_REF)
    content = file.decoded_content
    data = yaml.load(content, Loader=yaml.Loader)
    return data['repositories']


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
    # Precedence: an explicit --token-path always wins. Otherwise prefer the token
    # from the environment (e.g. an App installation token in CI, so nothing touches
    # disk), then fall back to the default token file.
    if path is None:
        env_token = os.environ.get(TOKEN_ENV_VAR)
        if env_token:
            return env_token.strip()
        path = DEFAULT_TOKEN_PATH
    with open(os.path.expanduser(path), "r") as input:
        token = input.readline()
        return token.strip()


def warning(message):
    """
    Prints a warning. In GitHub Actions the ::warning:: prefix turns it into an
    annotation on the run, so a skipped repository stays visible without making
    the run fail.
    """
    print(f'::warning::{message}')


def error(message):
    print(f'ERROR: {message}', file=sys.stderr)
    sys.exit(1)


if __name__ == '__main__':
    main()
