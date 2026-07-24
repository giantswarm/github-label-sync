[![Docker Repository on Quay](https://quay.io/repository/giantswarm/github-label-sync/status "Docker Repository on Quay")](https://quay.io/repository/giantswarm/github-label-sync)

# GitHub label synchronization utility

Synchronize labels from one repository (leader) to other repos.

The tool takes a source repository (the private `giantswarm/giantswarm`) and any number of target repositories (our public `giantswarm/roadmap` as well as customer repositories). Configured rules decide which labels are synchronized.

If the term "synchronization" raises high expectations: labels are actually copied in one direction only, from the leader repo to the target repo(s). If a label already exists in the target, it gets updated for color and description.

## Configuration

Take look at `config.yaml` for an example configuration file.

The `rules` part defines a list of rules, which get applied from first to last.

A rule applies if the label name fully matches the pattern (which is a Python regex). Partial matches are not enough.

If several rules apply, the last applying rule defines whether the label is included or ignored.

If a label (by its name) is not matched to any rule's pattern, it gets ignored (not synced).

## Usage

```nohighlight
docker build -t quay.io/giantswarm/github-label-sync .

docker run --rm -ti \
  -v $HOME:/home/user \
  quay.io/giantswarm/github-label-sync \
  --token-path /home/user/.github-token
```

The tool will present you all actions that it _would_ take, then you have to confirm that you want to proceed.

If used with `--dry-run`, no synchronization is happening and no confirmation is requested.

Use the `--conf` option to specify a configuration file path other than the default `./config.yaml`.

## Automated synchronization

Label sync runs automatically once a week (Mondays 06:00 UTC) via the
`.github/workflows/label-sync.yaml` GitHub Actions workflow, so target repos stay in sync
without anyone running the container by hand. It authenticates as the dedicated
`giantswarm-label-sync` GitHub App (installed org-wide, `contents:read` on the leader plus
`issues:write` on the targets) and runs `cli.py --yes`.

You can also trigger it manually from the **Actions** tab ("Sync labels to customer repos" →
Run workflow), with a `dry_run` toggle to preview the plan without applying any changes.

### Unattended flags

- `--yes` — apply the plan without the interactive confirmation prompt (for CI / cron runs).
- `GITHUB_TOKEN` env var — read the token from the environment instead of a `--token-path`
  file, so unattended runs need not write the secret to disk. `--token-path` still works for
  local use.
