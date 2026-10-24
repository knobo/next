import { expect, test } from 'claude-code/testing'

import { decide, gateOk, makeCfg, parse } from '../hooks/register'
import type { Env } from '../hooks/register'

const MERGE = '/home/knobo/prog/nextivo/nextivo-web-worktrees/merge'
const cfg = makeCfg()

function env(over: Partial<Env> & { gateCalls?: string[] } = {}): Env {
  const gateCalls = over.gateCalls ?? []
  return {
    cwd: async () => '/home/knobo/prog/nextivo',
    head: async () => ({ subject: 'Merge T-801: regenerate core OpenAPI contract', parents: 2 }),
    boardGate: async (t, dir) => {
      gateCalls.push(t + '@' + dir)
      return true
    },
    prodOpen: async () => false,
    inRepo: async () => true,
    mergeMatches: async () => true,
    ...over,
  }
}
const open = () => env({ prodOpen: async () => true })
const allowed = async (cmd: string, e: Env = env(), c = cfg) => (await decide(cmd, e, c))?.decision === 'allow'
const refused = async (cmds: string[], e: Env = env()) => {
  for (const c of cmds) expect(`${c} => ${await allowed(c, e)}`).toBe(`${c} => false`)
}
const approved = async (cmds: string[], e: Env = env()) => {
  for (const c of cmds) expect(`${c} => ${await allowed(c, e)}`).toBe(`${c} => true`)
}

// ---- parser ----------------------------------------------------------------

test('parse splits on && || ; | and newline and respects quotes', () => {
  expect(parse('a b && c | d; e "f && g"')?.map(s => s.argv)).toEqual([['a', 'b'], ['c'], ['d'], ['e', 'f && g']])
  expect(parse('a | b')?.map(s => s.piped)).toEqual([false, true])
  expect(parse('a |\nb')?.map(s => s.piped)).toEqual([false, true]) // a pipe continued on the next line is still a pipe
})

test('parse refuses everything shell-specific it does not model', () => {
  for (const bad of [
    'echo $(id)', 'echo `id`', 'echo $HOME', "echo $'x'", 'echo "a$b"', 'echo "a\\"b"', 'echo a\\\nb',
    'echo /*(e:id:)', 'echo {a,b}', 'echo ~', 'echo =ls', 'echo # c', 'echo x > /etc/passwd', 'sleep 1 &',
    'echo "unterminated', 'echo a | | b', 'a && && b', 'echo hi |', 'echo a\r', 'echo x >&9/../../f', 'echo x >/dev/nullX',
    'echo ~', 'echo ~/x', 'echo a:~1', 'echo x=~1', 'echo HEAD~', 'echo x 9>/dev/null', "echo x '2'>/dev/null", 'echo x >/dev/null\r', 'echo x |& cat',
  ]) {
    expect(`${bad} => ${JSON.stringify(parse(bad))}`).toBe(`${bad} => null`)
  }
})

test('parse accepts the harmless redirects and single-quoted metacharacters', () => {
  expect(parse('kubectl get pods 2>&1 | head -3')?.map(s => s.argv)).toEqual([['kubectl', 'get', 'pods'], ['head', '-3']])
  expect(parse('board status > /dev/null')?.map(s => s.argv)).toEqual([['board', 'status']])
  expect(parse('board status 2>/dev/null')?.map(s => s.argv)).toEqual([['board', 'status']])
  expect(parse("jq '.items[].name'")?.map(s => s.argv)).toEqual([['jq', '.items[].name']])
  expect(parse('echo "" x')?.map(s => s.argv)).toEqual([['echo', '', 'x']])
  expect(parse('a\tb')?.map(s => s.argv)).toEqual([['a', 'b']])
  expect(parse('git show HEAD~2')?.map(s => s.argv)).toEqual([['git', 'show', 'HEAD~2']])
})

// ---- kubectl ---------------------------------------------------------------

test('kubectl on the dev context is allowed, with stdin filters', async () => {
  await approved([
    'kubectl --context default -n queue rollout restart deploy/nextivo-web',
    'kubectl --context default -n board get pods', // reads work in every namespace
    'kubectl --context default -n queue get pods | head -5',
    'kubectl --context default -n queue get pods -o wide',
    'kubectl --context default -n queue logs deploy/x --tail 50',
    'kubectl --context default rollout status deploy/x -n queue --timeout 120s',
    'kubectl --context default -n queue delete pod x',
    'kubectl --context default -n queue delete pods,jobs -l app=x',
    "kubectl --context default -n queue patch deploy x --type json -p '[{\"op\":\"add\",\"path\":\"/a\",\"value\":1}]'",
    'kubectl --context default -n queue set image deploy/x c=img:1',
    'kubectl --context default -n queue exec pod-1 -- ls /tmp',
  ])
})

test('kubectl: unknown verbs, missing context, files, secrets, RBAC and mass deletes are not approved', async () => {
  await refused([
    'kubectl -n board get pods',
    'kubectl --context default apply -k overlays/dev',
    'kubectl --context default cp pod:/etc/passwd /tmp/p',
    'kubectl --context default create -f x.yaml',
    'kubectl --context default create secret generic x',
    'kubectl --context default create clusterrolebinding x',
    'kubectl --context default delete namespace queue',
    'kubectl --context default delete secret board',
    'kubectl --context default delete pods --all',
    'kubectl --context default delete pods --all=true',
    'kubectl --context default delete pods -A',
    'kubectl --context default -n board get secret board -o yaml',
    'kubectl --context default get secrets,pods -o yaml',
    'kubectl --context default get secret/x -o jsonpath=x',
    'kubectl --context default get --raw /api/v1/secrets',
    'kubectl --context default patch secret x -p x -o yaml',
    'kubectl --context default set env deploy/x --from=secret/db',
    'kubectl --context default label ns kube-system a=b',
    'kubectl --context default delete -f x.yaml',
    'kubectl --context default port-forward pod 80:80 --address 0.0.0.0',
  ])
})

test('kubectl: flag values and unknown flags cannot shift the verb or the resource (F1, F2, F4)', async () => {
  await refused([
    'kubectl --context default delete -n queue namespace foo', // value flag after the verb must not become the resource
    'kubectl --context default delete pod/x namespace/queue',
    'kubectl --context default delete pods,ns -l a=b',
    'kubectl --context default delete pvc,deploy x',
    'kubectl --context default --cache-dir get apply -k x', // unknown flag before the verb
    'kubectl --context default --request-timeout 5s get pods',
    'kubectl --context default --cluster x get pods',
    'kubectl --context default --kubeconfig /tmp/x get pods',
    'kubectl --context default --server https://evil get pods',
    'kubectl --context default -s https://evil get pods',
    'kubectl --context default --context prod get pods',
    'kubectl --context default -n get plugin-thing',
  ])
  await refused(['kubectl --context prod --cache-dir get delete deploy x', 'kubectl --context prod --cache-dir status undo deploy x'], open())
  await approved(['kubectl --context default -n queue exec p -- ls --context=prod']) // after `--` is the pod's business
  await approved(['kubectl --context default rollout -n queue restart deploy/x'])
  await approved(['kubectl --context default -n queue patch deployments.apps/x -p x', 'kubectl --context default -n queue label pods a=b'])
})

test('kubectl: prod follows the gate, reads no secrets and cannot write', async () => {
  const cmd = 'kubectl --context prod -n queue rollout restart deploy/nextivo-web'
  expect(await allowed(cmd)).toBe(false)
  await approved([cmd, 'kubectl --context prod -n queue get pods', 'kubectl --context prod -n queue rollout status deploy/x'], open())
  await refused([
    'kubectl --context prod delete pod x', 'kubectl --context prod get secret x -o yaml', 'kubectl --context prod rollout undo deploy/x',
    'kubectl --context prod exec p -- ls', 'kubectl --context prod patch deploy x -p x',
  ], open())
})

// ---- git -------------------------------------------------------------------

test('push to main: only `git -C <merge worktree> push origin HEAD:main`, a real merge commit, a passing board gate', async () => {
  const push = `git -C ${MERGE} push origin HEAD:main`
  const gateCalls: string[] = []
  expect(await allowed(push, env({ gateCalls }))).toBe(true)
  expect(gateCalls).toEqual(['T-801@' + MERGE]) // head and gate run in the repo that is pushed
  await refused(['git push origin HEAD:main']) // no -C: the session cwd is not trusted
  expect(await allowed(push, env({ boardGate: async () => false }))).toBe(false)
  expect(await allowed(push, env({ head: async () => ({ subject: 'wip', parents: 2 }) }))).toBe(false)
  expect(await allowed(push, env({ head: async () => ({ subject: 'Merge T-1', parents: 1 }) }))).toBe(false)
  expect(await allowed(push, env({ head: async () => ({ subject: 'x Merge T-1', parents: 2 }) }))).toBe(false)
  await refused([`git -C /home/knobo/prog/other push origin HEAD:main`, `git -C /home/knobo/prog/nextivo/x-worktrees/merge-evil push origin HEAD:main`])
  await refused([`git merge evil && git -C ${MERGE} push origin HEAD:main`, `cd ${MERGE} && git -C ${MERGE} push origin HEAD:main`])
})

test('refspecs that could reach main another way are not approved', async () => {
  await refused([
    'x:main', 'HEAD', 'refs/heads/*:refs/heads/*', 'HEAD:refs/heads/*', 'HEAD:heads/main', '@:main', ':old', '+HEAD:main',
    'HEAD:refs/heads//main', 'HEAD:master', 'HEAD:main:extra',
  ].map(r => `git -C ${MERGE} push origin ${r}`))
  await refused([`git -C ${MERGE} push --force origin HEAD:main`, `git -C ${MERGE} push origin --all`, `git -C ${MERGE} push evil HEAD:main`, `git -C ${MERGE} push --delete origin x`, `git -C ${MERGE} push -o x origin HEAD:main`])
})

test('tags of any kind follow the prod gate', async () => {
  for (const c of ['git push origin v0.3.3', 'git push origin vfoo', 'git push origin refs/tags/v1:refs/tags/v1', 'git push origin --tags']) {
    expect(await allowed(c)).toBe(false)
    expect(await allowed(c, open())).toBe(true)
  }
  await refused(['git push origin refs/tags/*:refs/tags/*'], open())
})

test('feature branches and local git work are allowed', async () => {
  await approved([
    'git push origin task/T-801', 'git push -u origin task/T-801', 'git push --set-upstream forgejo task/T-801',
    'git fetch origin && git merge --no-ff task/T-801', 'git fetch origin main', 'git pull --ff-only origin main',
    'git -C /home/knobo/prog/next status', 'git --no-pager log -3', 'git log --oneline origin/main..HEAD', 'git diff --stat HEAD~1',
    'git checkout -b task/T-9', 'git switch task/T-9', 'git switch -c task/T-9 origin/main', 'git add -A', 'git add src/a.ts',
    'git commit -m "feat: x"', 'git commit -am "fix: y"', 'git rebase origin/main', 'git reset --soft HEAD~1', 'git branch', 'git branch -a',
    'git remote -v', 'git rev-parse --abbrev-ref HEAD', 'git merge-base --is-ancestor a b', 'git worktree list',
    'git worktree add ../x-worktrees/task/T-1 -b task/T-1 origin/main',
  ])
})

test('git cannot run programs, repoint remotes, read other files or discard work (F5, F6, F8, F11)', async () => {
  await refused([
    'git -c core.sshCommand=evil fetch origin', 'git --exec-path=/tmp status', 'git -C /etc status', 'git -C /home/knobo/prog/a -C /etc status',
    'git fetch --upload-pack=evil origin', 'git fetch /tmp/evil', 'git ls-remote /tmp/evil', 'git pull origin main',
    'git rebase -x evil main', 'git rebase -xevil main', 'git rebase --exe=evil main', 'git rebase -i HEAD~2',
    'git grep -O evil x', 'git grep -iOevil x', 'git grep --open-f=evil x', 'git grep --no-index x /etc',
    'git diff --output=/tmp/x', 'git diff --no-index /etc/passwd /dev/null', 'git diff /etc/passwd /dev/null', 'git blame --contents=/etc/passwd f', 'git blame --conten=/etc/passwd f',
    'git remote -v set-url origin https://evil', 'git remote set-url origin x', 'git remote add evil /tmp/x', 'git remote show -n',
    'git checkout .', 'git checkout src', 'git checkout main', 'git restore src', 'git reset --hard origin/main', 'git reset --har HEAD', 'git reset --merge',
    'git branch -D x', 'git branch -f main HEAD~3', 'git branch --delete --force x', 'git branch -M x', 'git switch -C x', 'git switch --discard-changes x',
    'git worktree remove --force x', 'git worktree remove --forc x', 'git worktree add /tmp/x', 'git tag -d v1', 'git tag v9', 'git clean -fdx',
    'git commit -F /etc/passwd', 'git commit --no-verify -m x', 'git add /etc/passwd', 'git log ../../etc/passwd', 'git show /etc/passwd',
  ])
})

// ---- other programs --------------------------------------------------------

test('board: lifecycle and gate are fine; identity, state edits and prod deploys are not (F13)', async () => {
  await approved(['board gate merge T-1', 'board task show T-1 | jq .status', 'board task progress T-1 "note"', 'board status --me'])
  await refused([
    'board task patch T-1 risk=low', 'board task deploy T-1 --env prod', 'board grant x', 'board task claim T-1 --human-token x',
    'board register', 'board task review T-1 --open 0', 'export BOARD_SESSION=x', 'export BOARD_AGENT_ID=x',
  ])
})

test('tea: no merge, approve, other login or auto-merge labels (F13)', async () => {
  await approved(['tea pr list --repo nextivo/x', 'tea pr 138 --repo nextivo/x', 'tea pr create --title x --repo nextivo/x'])
  await refused([
    'tea pr merge 1', 'tea pr m 1', 'tea pr approve 1', 'tea pr clean 1', 'tea pr list --login other',
    'tea pr create --labels automerge', 'tea pr create -L automerge', 'tea pr create --labels=automerge',
  ])
})

test('stdin filters read only stdin: no files, no exec flags, must be piped (S1, F12)', async () => {
  await approved([
    'kubectl --context default get pods | jq .items | head -5', 'kubectl --context default get pods | grep Running | wc -l',
    "kubectl --context default get pods -o json | jq '.items[].metadata.name'",
  ])
  await refused([
    'jq . /home/knobo/.kube/config', 'head /home/knobo/.ssh/id_ed25519', 'grep -r secret /home/knobo/.config',
    'echo x | grep -e. /etc/passwd', 'echo x | grep -f/etc/passwd x', 'echo x | sort --output=/tmp/x', 'echo x | sort -S1 --compress-program=sh',
    'echo x | jq -n env', 'echo x | jq $ENV', 'rg --pre ./evil x', 'echo x | wc --files0-from=/etc/passwd', 'grep x',
    'sed -n "w out" f', 'echo x | date -f /etc/passwd', 'echo x | constructor', 'echo x | toString', 'echo x | __proto__',
  ])
})

test('cd, env prefixes, secrets, unknown programs and ssh', async () => {
  await approved(['cd /home/knobo/prog/next && git status', 'ls', 'ls -la src', 'pwd'])
  await refused([
    'export PATH=/tmp/evil', 'FOO=x git status', 'cd /home/knobo/prog/../../etc', 'cd /tmp', 'cat /home/knobo/.kube/config', 'ls /etc', 'ls ../..',
    'echo forgejo-token', 'echo x | jq .a /home/knobo/.ssh/x', 'pass board/forgejo-token | head -1',
    'rm -rf /', 'curl https://example.com', 'ssh root@nextivo.app ls', 'printf -v PATH x', 'rtk proxy rm x', 'git status && curl https://example.com',
  ], open())
  await approved(['git status && board task show T-1 | head -3'])
})

test('rtk-rewritten commands are looked through, but only for the known programs', async () => {
  await approved(['rtk git status', 'rtk kubectl --context default get pods'])
  await refused(['rtk err rm -rf x', 'rtk read /etc/passwd'])
})

test('configuration changes what is approved', async () => {
  const other = makeCfg({ devContext: 'k3s-dev', remotes: 'origin', mainBranch: 'trunk', progRoot: '/srv/code' })
  expect(await allowed('kubectl --context k3s-dev get pods', env(), other)).toBe(true)
  expect(await allowed('kubectl --context default get pods', env(), other)).toBe(false)
  expect(await allowed('git fetch forgejo', env(), other)).toBe(false)
  expect(await allowed('cd /srv/code/app', env(), other)).toBe(true)
  expect(await allowed('cd /home/knobo/prog/next', env(), other)).toBe(false)
})

test('the board gate answer is read as JSON, not just as an exit code', () => {
  expect(gateOk(0, '{"ok": true, "reasons": []}')).toBe(true)
  expect(gateOk(0, '{"ok": false, "reasons": ["x"]}')).toBe(false)
  expect(gateOk(0, 'queued write, exit 0')).toBe(false)
  expect(gateOk(1, '{"ok": true}')).toBe(false)
})

// ---- through the engine ------------------------------------------------------

test('hook approves an allowlisted call the engine would ask about', async ($, on) => {
  on('tool.check', { tool: 'Bash' }, () => ({ decision: 'ask' }))
  const r = await $.tool.check({ tool: 'Bash', input: { command: 'kubectl --context default -n board get pods' } })
  expect(r.decision).toBe('allow')
})

test('hook leaves a non-allowlisted call to the engine', async ($, on) => {
  on('tool.check', { tool: 'Bash' }, () => ({ decision: 'ask' }))
  const r = await $.tool.check({ tool: 'Bash', input: { command: 'curl https://example.com' } })
  expect(r.decision).toBe('ask')
})

test('hook never overrides a deny', async ($, on) => {
  on('tool.check', { tool: 'Bash' }, () => ({ decision: 'deny', reason: 'rule' }))
  const r = await $.tool.check({ tool: 'Bash', input: { command: 'kubectl --context default get pods' } })
  expect(r.decision).toBe('deny')
})

test('hook respects an ask that comes from a settings rule or a hook (F9)', async ($, on) => {
  on('tool.check', { tool: 'Bash' }, () => ({ decision: 'ask', rule: 'Bash(kubectl:*)' }))
  expect((await $.tool.check({ tool: 'Bash', input: { command: 'kubectl --context default get pods' } })).decision).toBe('ask')
})

test('hook does not approve a call that asks to leave the sandbox', async ($, on) => {
  on('tool.check', { tool: 'Bash' }, () => ({ decision: 'ask' }))
  const r = await $.tool.check({ tool: 'Bash', input: { command: 'kubectl --context default get pods', dangerouslyDisableSandbox: true } })
  expect(r.decision).toBe('ask')
})

test('review 3: pull/ls-remote/branch ignore nothing after `--` (R3-1)', async () => {
  await refused([
    "git pull --ff-only -- '--upload-pack=x' /tmp/r", "git ls-remote origin -- '--upload-pack=x'", 'git fetch origin -- x', 'git branch -- x y',
    "git push origin task/x -- '--receive-pack=x'",
  ])
})

test('review 3: board start/register/review change identity and are not approved (R3-2)', async () => {
  await refused(['board start reviewer', 'board start --sh', 'board register', 'board task review T-1 --open 0'])
})

test('review 3: writes and exec only in the configured dev namespaces (R3-3)', async () => {
  await refused([
    'kubectl --context default -n board exec board-0 -- env', 'kubectl --context default -n board patch deploy board -p x',
    'kubectl --context default -n kube-system delete pod x', 'kubectl --context default exec p -- ls', // no -n at all
    'kubectl --context default -n board rollout restart deploy/board', 'kubectl --context default -n board scale deploy/x --replicas 0',
  ])
  await approved(['kubectl --context default -n board rollout status deploy/board', 'kubectl --context default -n board logs deploy/board'])
  const wide = makeCfg({ devWriteNamespaces: 'queue,board' })
  expect(await allowed('kubectl --context default -n board rollout restart deploy/board', env(), wide)).toBe(true)
})

test('review 3: git diff needs a work tree and no preceding cd (R3-4)', async () => {
  expect(await allowed('git diff --stat HEAD~1', env({ inRepo: async () => false }))).toBe(false)
  expect(await allowed('git -C /home/knobo/prog diff a b', env({ inRepo: async () => false }))).toBe(false)
  await refused(['cd /home/knobo/prog && git diff a b'])
  await approved(['git fetch origin && git diff --stat origin/main'])
})

test('review 3: every kubectl target is checked against an allowlist of workload types (R3-5)', async () => {
  await refused([
    'kubectl --context default -n queue label pod/x ns/kube-system a=b', 'kubectl --context default -n queue patch deploy/x clusterrolebinding/admin -p x',
    'kubectl --context default -n queue patch clusterrolebindings.rbac.authorization.k8s.io/x -p x', 'kubectl --context default -n queue label no x a=b',
    'kubectl --context default -n queue patch sa x -p x', 'kubectl --context default -n queue label validatingwebhookconfigurations x a=b',
    'kubectl --context default -n queue delete pod/x ns/queue', 'kubectl --context default -n queue delete pods x/y',
  ])
})

test('review 3: worktree paths are resolved against the right base and never after a cd (R3-6)', async () => {
  await refused([
    'git -C /home/knobo/prog/nextivo worktree add ../../../../tmp/evil', 'cd /home/knobo/prog && git worktree add ../../../tmp/evil',
    'git worktree add /tmp/evil', 'git worktree add ../x-worktrees/a -b "bad name"', 'cd /home/knobo/prog/nextivo && git worktree add ../x-worktrees/a',
  ])
  await approved(['git worktree add /home/knobo/prog/x-worktrees/task/T-1 -b task/T-1 origin/main'])
})

test('review 3: flags that only take =value or an optional value do not swallow the next operand (R3-7)', async () => {
  await refused(['git log --pretty /etc/passwd', 'git log --format x', 'git diff --unified 3 /etc/hosts', 'git describe --abbrev /etc/passwd', 'git for-each-ref --format x'])
  await approved(['git log --pretty=oneline', 'git log --format=%s -n 3', 'git describe --tags --abbrev=7'])
})

test('review 3: ls takes only plain flags and secret-ish names stay out (R3-9)', async () => {
  await refused(['ls --color=always', 'ls .kube', 'ls .ssh', 'ls /home/knobo/.aws', 'ls .env'])
  await approved(['ls -la', 'ls -1 src'])
})

test('review 4: --wait is a switch, so a token after it is a target and is checked (R4-1)', async () => {
  await refused([
    'kubectl --context default -n queue delete pod/x --wait namespace/queue', 'kubectl --context default -n queue delete pod/x --wait crd/x',
    'kubectl --context default -n queue delete pod x --wait secret/x',
  ])
  await approved(['kubectl --context default -n queue delete pod x --wait=false'])
})

test('review 4: -n and --namespace are one flag; the last wins and mixed spellings are refused (R4-2)', async () => {
  await refused([
    'kubectl --context default -n queue exec p --namespace kube-system -- ls', 'kubectl --context default exec p -n queue --namespace=kube-system -- ls',
    'kubectl --context default -n queue delete pod x -n kube-system', 'kubectl --context default --namespace queue patch deploy/x -n board -p x',
  ])
  await approved(['kubectl --context default -n board rollout restart deploy/x -n queue']) // last one wins in kubectl: queue
})

test('review 4: the main push must merge exactly the reviewed branch into the current main (R4-3)', async () => {
  const push = `git -C ${MERGE} push origin HEAD:main`
  expect(await allowed(push, env({ mergeMatches: async () => false }))).toBe(false) // amended, wrong parents, or a different branch
  expect(await allowed(push, env({ head: async () => ({ subject: "Merge branch 'task/T-801'", parents: 2 }) }))).toBe(true) // the skill's own merge message
})

test('review 4: git diff cannot be turned into a two-file compare (R4-4)', async () => {
  await refused(['git diff /home/knobo/prog/x/.env /home/knobo/prog/x/y', 'git -C /home/knobo/prog/x diff /a /b', 'git diff -- /home/knobo/prog/x/key'])
  await approved(['git diff --stat -- src', 'git log -- src/a.ts', 'git -C /home/knobo/prog/x diff', 'cd /home/knobo/prog/x && git -C /home/knobo/prog/x diff'])
})

test('review 4: absolute paths cannot use `..` (symlinks resolve it differently from text) (R4-5)', async () => {
  await refused(['cd /home/knobo/prog/projects/../x', 'git -C /home/knobo/prog/a/../b status', 'ls /home/knobo/prog/a/../b'])
})

test('review 4: label/env removals are not targets (regression)', async () => {
  await approved(['kubectl --context default -n queue label deploy/x foo-', 'kubectl --context default -n queue set env deploy/x FOO-', 'kubectl --context default -n queue label deploy/x app.kubernetes.io/name=x'])
})
