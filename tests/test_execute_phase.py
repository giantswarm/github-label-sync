"""
Tests for the execute phase, with a stubbed GitHub client that keeps PyGithub's
own argument checks. The first scheduled run (37273989839) crashed there with
`AssertionError: None` from create_label, because every earlier CI run had been
a dry run.
"""
import types

import github
import yaml
from click.testing import CliRunner
from github.GithubObject import NotSet, is_optional

import cli


class Label:
    def __init__(self, name, color, description, calls):
        self.name, self.color, self.description, self._calls = name, color, description, calls

    def edit(self, name, color, description=NotSet):
        assert isinstance(name, str), name
        assert isinstance(color, str), color
        assert is_optional(description, str), description  # same check as PyGithub Label.edit
        self._calls.append(('edit', self.name, description))


class Repo:
    def __init__(self, full_name, labels, calls, archived=False):
        self.full_name, self._labels, self._calls, self.archived = full_name, labels, calls, archived

    def get_labels(self):
        return [Label(n, c, d, self._calls) for (n, c, d) in self._labels]

    def create_label(self, name, color, description=NotSet):
        assert isinstance(name, str), name
        assert isinstance(color, str), color
        assert is_optional(description, str), description  # same check as PyGithub Repository.create_label
        self._calls.append(('create', self.full_name, name, description))

    def get_contents(self, path, ref):
        customers = {'repositories': [{'organization': 'giantswarm', 'repository': 'customer-a'}]}
        return types.SimpleNamespace(decoded_content=yaml.dump(customers).encode())


class Requester:
    def requestJsonAndCheck(self, verb, url, parameters=None):
        return {}, {'total_count': 0, 'repository_selection': 'all', 'repositories': []}


class StubGithub:
    def __init__(self, token, repos, calls):
        self._repos, self._calls = repos, calls
        self.requester = Requester()

    def get_repo(self, full_name):
        self._calls.append(('get_repo', full_name))
        return self._repos[full_name]


def run(tmp_path, monkeypatch, leader_labels, target_labels, customer_labels, args, repo_class=Repo):
    calls = []
    repos = {
        'giantswarm/giantswarm': repo_class('giantswarm/giantswarm', leader_labels, calls),
        'giantswarm/roadmap': repo_class('giantswarm/roadmap', target_labels, calls),
        'giantswarm/customer-a': repo_class('giantswarm/customer-a', customer_labels, calls),
    }
    monkeypatch.setattr(cli.github, 'Github', lambda token: StubGithub(token, repos, calls))
    monkeypatch.setattr(cli.time, 'sleep', lambda seconds: None)
    monkeypatch.setenv('GITHUB_TOKEN', 'x')
    conf = tmp_path / 'config.yaml'
    conf.write_text(yaml.dump({
        'github': {'organization': 'giantswarm', 'repositories': [{'name': 'giantswarm', 'leader': True}, {'name': 'roadmap'}]},
        'rules': [{'description': 'area', 'mode': 'include', 'pattern': 'area/.*'}],
    }))
    result = CliRunner().invoke(cli.main, ['--conf', str(conf)] + args)
    writes = [c for c in calls if c[0] in ('create', 'edit')]
    return result, calls, writes


def test_create_with_none_description_does_not_abort(tmp_path, monkeypatch):
    result, _, writes = run(tmp_path, monkeypatch,
                            leader_labels=[('area/docs', 'aaaaaa', None), ('area/kaas', 'bbbbbb', 'KaaS')],
                            target_labels=[], customer_labels=[('area/kaas', 'bbbbbb', 'KaaS')], args=['--yes'])
    assert result.exit_code == 0, result.output
    assert ('create', 'giantswarm/roadmap', 'area/docs', '') in writes
    assert ('create', 'giantswarm/roadmap', 'area/kaas', 'KaaS') in writes
    assert ('create', 'giantswarm/customer-a', 'area/docs', '') in writes
    assert len(writes) == 3


def test_edit_clears_a_stale_description(tmp_path, monkeypatch):
    result, _, writes = run(tmp_path, monkeypatch,
                            leader_labels=[('area/docs', 'aaaaaa', None)],
                            target_labels=[('area/docs', 'aaaaaa', 'old')], customer_labels=[('area/docs', 'aaaaaa', None)],
                            args=['--yes'])
    assert result.exit_code == 0, result.output
    assert writes == [('edit', 'area/docs', '')]


def test_null_and_empty_description_are_in_sync(tmp_path, monkeypatch):
    # The API returns both null and "" for a label without description.
    result, _, writes = run(tmp_path, monkeypatch,
                            leader_labels=[('area/docs', 'aaaaaa', None), ('area/kaas', 'bbbbbb', '')],
                            target_labels=[('area/docs', 'aaaaaa', ''), ('area/kaas', 'bbbbbb', None)],
                            customer_labels=[('area/docs', 'aaaaaa', None), ('area/kaas', 'bbbbbb', '')], args=['--yes'])
    assert result.exit_code == 0, result.output
    assert 'Everything in sync' in result.output
    assert writes == []


def test_one_failing_job_does_not_stop_the_rest_but_ends_red(tmp_path, monkeypatch):
    class FlakyRepo(Repo):
        def create_label(self, name, color, description=NotSet):
            if self.full_name == 'giantswarm/roadmap' and name == 'area/docs':
                raise github.GithubException(422, {'message': 'Validation Failed'}, None)
            return super().create_label(name, color, description)

    result, _, writes = run(tmp_path, monkeypatch,
                            leader_labels=[('area/docs', 'aaaaaa', None), ('area/kaas', 'bbbbbb', 'KaaS')],
                            target_labels=[], customer_labels=[], args=['--yes'], repo_class=FlakyRepo)
    assert result.exit_code == 1, result.output
    assert '1 of 4 label operations failed' in result.output
    assert len(writes) == 3


def test_failed_repo_fetch_is_remembered(tmp_path, monkeypatch):
    class GoneInExecute(StubGithub):
        def get_repo(self, full_name):
            self._calls.append(('get_repo', full_name))
            if full_name == 'giantswarm/roadmap' and self._calls.count(('get_repo', full_name)) > 1:
                raise github.GithubException(500, {'message': 'Server Error'}, None)
            return self._repos[full_name]

    calls = []
    repos = {
        'giantswarm/giantswarm': Repo('giantswarm/giantswarm', [('area/docs', 'aaaaaa', None), ('area/kaas', 'bbbbbb', 'KaaS')], calls),
        'giantswarm/roadmap': Repo('giantswarm/roadmap', [], calls),
        'giantswarm/customer-a': Repo('giantswarm/customer-a', [], calls),
    }
    monkeypatch.setattr(cli.github, 'Github', lambda token: GoneInExecute(token, repos, calls))
    monkeypatch.setattr(cli.time, 'sleep', lambda seconds: None)
    monkeypatch.setenv('GITHUB_TOKEN', 'x')
    conf = tmp_path / 'config.yaml'
    conf.write_text(yaml.dump({
        'github': {'organization': 'giantswarm', 'repositories': [{'name': 'giantswarm', 'leader': True}, {'name': 'roadmap'}]},
        'rules': [{'description': 'area', 'mode': 'include', 'pattern': 'area/.*'}],
    }))
    result = CliRunner().invoke(cli.main, ['--conf', str(conf), '--yes'])
    assert result.exit_code == 1, result.output
    assert '2 of 4 label operations failed' in result.output
    assert 'could not be fetched earlier in this run' in result.output
    # read phase + one failed attempt in the execute phase, no retry for the second job
    assert calls.count(('get_repo', 'giantswarm/roadmap')) == 2
    writes = [c for c in calls if c[0] in ('create', 'edit')]
    assert writes == [('create', 'giantswarm/customer-a', 'area/docs', ''), ('create', 'giantswarm/customer-a', 'area/kaas', 'KaaS')]


def test_max_jobs_defers_the_rest_and_stays_green(tmp_path, monkeypatch):
    result, _, writes = run(tmp_path, monkeypatch,
                            leader_labels=[('area/docs', 'aaaaaa', None), ('area/kaas', 'bbbbbb', 'KaaS')],
                            target_labels=[], customer_labels=[], args=['--yes', '--max-jobs', '3'])
    assert result.exit_code == 0, result.output
    assert '::notice::1 of 4 label operations deferred to the next run (--max-jobs 3).' in result.output
    assert len(writes) == 3
    # The full plan is still printed.
    assert result.output.count(': create label ') >= 4 + 3


def test_max_jobs_zero_means_no_limit(tmp_path, monkeypatch):
    result, _, writes = run(tmp_path, monkeypatch,
                            leader_labels=[('area/docs', 'aaaaaa', None), ('area/kaas', 'bbbbbb', 'KaaS')],
                            target_labels=[], customer_labels=[], args=['--yes', '--max-jobs', '0'])
    assert result.exit_code == 0, result.output
    assert '::notice::' not in result.output
    assert len(writes) == 4


def test_dry_run_prints_plan_and_deferral_without_writes(tmp_path, monkeypatch):
    result, _, writes = run(tmp_path, monkeypatch,
                            leader_labels=[('area/docs', 'aaaaaa', None), ('area/kaas', 'bbbbbb', 'KaaS')],
                            target_labels=[], customer_labels=[], args=['--dry-run', '--max-jobs', '1'])
    assert result.exit_code == 0, result.output
    assert '- giantswarm/roadmap: create label area/docs' in result.output
    assert '::notice::3 of 4 label operations deferred' in result.output
    assert writes == []
