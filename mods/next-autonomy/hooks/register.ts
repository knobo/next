import type { Register } from 'claude-code'

// ---- configuration ---------------------------------------------------------
// Everything project-specific comes from the manifest's userConfig (settings.json
// `pluginConfigs["next-autonomy"].options`), so the mod is not tied to one project.

export type Cfg = {
  progRoot: string
  devContext: string
  devWriteNamespaces: Set<string>
  remotes: Set<string>
  mainRemote: string
  mainBranch: string
  mergeWorktree: RegExp
  prodGateFile: string
}

export const DEFAULTS = {
  progRoot: '/home/knobo/prog/',
  devContext: 'default',
  devWriteNamespaces: 'queue',
  remotes: 'origin,forgejo',
  mainRemote: 'origin',
  mainBranch: 'main',
  mergeWorktreeRegex: '^/home/knobo/prog/(?:[^/]+/)?[^/]+-worktrees/merge/?$',
  prodGateFile: '/home/knobo/.claude/next-prod-gate',
}

export function makeCfg(o: Readonly<Record<string, unknown>> = {}): Cfg {
  const s = (k: keyof typeof DEFAULTS) => (typeof o[k] === 'string' && o[k] !== '' ? (o[k] as string) : DEFAULTS[k])
  return {
    progRoot: s('progRoot').replace(/\/?$/, '/'),
    devContext: s('devContext'),
    devWriteNamespaces: new Set(s('devWriteNamespaces').split(',').map(x => x.trim()).filter(Boolean)),
    remotes: new Set(s('remotes').split(',').map(x => x.trim()).filter(Boolean)),
    mainRemote: s('mainRemote'),
    mainBranch: s('mainBranch'),
    mergeWorktree: new RegExp(s('mergeWorktreeRegex')),
    prodGateFile: s('prodGateFile'),
  }
}

const TIMEOUT_MS = 10_000

export type Env = {
  cwd: () => Promise<string>
  /** subject and parent count of HEAD in the repo at `dir` */
  head: (dir: string) => Promise<{ subject: string; parents: number }>
  /** the merge commit is exactly what the board judged: HEAD^1 is <remote>/<main>, HEAD^2 is the tip of <remote>/task/<id>, and the tree equals a clean merge of the two */
  mergeMatches: (dir: string, taskId: string) => Promise<boolean>
  /** true only when `board gate merge <id>` answered ok:true */
  boardGate: (taskId: string, dir: string) => Promise<boolean>
  prodOpen: () => Promise<boolean>
  /** true when `dir` is inside a git work tree (outside one, `git diff a b` compares arbitrary files) */
  inRepo: (dir: string) => Promise<boolean>
}
export type Verdict = { decision: 'allow'; reason: string } | null
export type Seg = { argv: string[]; piped: boolean }

// ---- parsing -------------------------------------------------------------
//
// The shell the command really runs in may be zsh or bash. Instead of modelling either, this
// parser accepts a small, boring subset and returns null (= defer to the classifier) for
// everything else: no backslash anywhere, no `$`, backtick, glob, brace, tilde, `=word`, `#`
// or `!` outside single quotes, no control characters, only the exact harmless redirects.

const SAFE_UNQUOTED = /[A-Za-z0-9_\-.,/:=@%+]/

export function parse(cmd: string): Seg[] | null {
  const segs: Seg[] = []
  let cur: string[] = []
  let piped = false
  let tok = ''
  let has = false
  let quoted = false
  let q: '' | "'" | '"' = ''

  const pushTok = () => {
    if (has) cur.push(tok)
    tok = ''
    has = false
    quoted = false
  }
  // closes the current segment; false = an empty segment (`a && && b`), a syntax error
  const endSeg = (isNewline: boolean, nextPiped: boolean): boolean => {
    pushTok()
    if (cur.length === 0) {
      if (!isNewline) return false
      return true // a blank line, or the newline after `|`: `piped` carries over
    }
    segs.push({ argv: cur, piped })
    cur = []
    piped = nextPiped
    return true
  }

  for (let i = 0; i < cmd.length; i++) {
    const c = cmd[i]
    const code = c.charCodeAt(0)
    if ((code < 32 && c !== '\n' && c !== '\t') || code === 127) return null
    if (q === "'") {
      if (c === "'") q = ''
      else tok += c
      continue
    }
    if (q === '"') {
      if (c === '"') q = ''
      else if (c === '$' || c === '\\' || c === '`') return null
      else tok += c
      continue
    }
    if (c === "'" || c === '"') {
      q = c
      has = true
      quoted = true
      continue
    }
    if (c === ' ' || c === '\t') {
      pushTok()
      continue
    }
    if (c === '\n') {
      if (!endSeg(true, false)) return null
      continue
    }
    if (c === ';') {
      if (!endSeg(false, false)) return null
      continue
    }
    if (c === '&' || c === '|') {
      const double = cmd[i + 1] === c
      if (c === '&' && !double) return null // background
      if (double) i++
      if (!endSeg(false, c === '|' && !double)) return null
      if (cmd.slice(i + 1).trim() === '') return null // dangling operator
      continue
    }
    if (c === '>') {
      const m = /^>{1,2}[ \t]*\/dev\/null(?=[ \t\n;&|]|$)|^>&[12](?=[ \t\n;&|]|$)/.exec(cmd.slice(i))
      if (!m) return null
      if (has && /^\d+$/.test(tok)) {
        // a file-descriptor number: only an unquoted 1 or 2 is the redirect's own prefix
        if (quoted || (tok !== '1' && tok !== '2')) return null
        tok = ''
        has = false
      } else if (has) pushTok()
      i += m[0].length - 1
      continue
    }
    // `HEAD~1`: a tilde in the middle of a plain word is literal in both shells; at the start of a word,
    // or after `=` or `:`, it expands to a home directory, so those stay refused.
    if (c === '~' && /^[A-Za-z0-9_./-]*[A-Za-z0-9_]$/.test(tok) && /[0-9]/.test(cmd[i + 1] ?? '')) {
      tok += c
      continue
    }
    if (!SAFE_UNQUOTED.test(c)) return null
    if (c === '=' && !has) return null // zsh `=cmd` expansion
    tok += c
    has = true
  }
  if (q) return null
  pushTok()
  if (cur.length) segs.push({ argv: cur, piped })
  return segs.length ? segs : null
}

// ---- helpers -------------------------------------------------------------

type R = Verdict | 'ask' | 'prod'

function ok(reason: string): Verdict {
  return { decision: 'allow', reason: 'next-autonomy: ' + reason }
}

const isNum = (t: string) => /^\d+$/.test(t)
const OPERAND = /^[\w.:/,@=%+-]+$/
const has = (o: object, k: string) => Object.prototype.hasOwnProperty.call(o, k)

// Normalise a path (no `..` escapes) without Node's `path`.
function normPath(p: string): string {
  const out: string[] = []
  for (const part of p.split('/')) {
    if (part === '' || part === '.') continue
    if (part === '..') out.pop()
    else out.push(part)
  }
  return '/' + out.join('/')
}
const underRoot = (p: string, cfg: Cfg) => p.startsWith('/') && !p.split('/').includes('..') && (normPath(p) + '/').startsWith(cfg.progRoot) // `..` is resolved through symlinks by the OS, not textually

// A path or revision operand: absolute only below the root, no `..` path segment.
// (`a..b` revision ranges are one segment and pass.)
const pathOk = (p: string, cfg: Cfg) => (p.startsWith('/') ? underRoot(p, cfg) : !p.split('/').includes('..'))

const SECRET_WORDS = /forgejo-token|human_token|HUMAN_TOKEN|\.gnupg|\.password-store|\.ssh\b|\.kube\b|\.claude\b|\.config\/tea|\.aws\b|\.docker\b|\.netrc|\.env\b/

// ---- flag scanner ----------------------------------------------------------
// Principle: every program is approved only with flags it is known to need.
// 'b' = no value, 'v' = value (`--flag=x` or `--flag x`), 'a' = value only as `--flag=x`, 'o' = optional `=x`
// (never consumes the next token). An unknown flag means "not approved".

type Flags = Record<string, 'b' | 'v' | 'a' | 'o'>
type Scan = { operands: string[]; flags: Map<string, string | true>; rest: string[] }

function scan(args: string[], spec: Flags, numFlag = false): Scan | null {
  const operands: string[] = []
  const flags = new Map<string, string | true>()
  for (let i = 0; i < args.length; i++) {
    const t = args[i]
    if (t === '--') return { operands, flags, rest: args.slice(i + 1) }
    if (!t.startsWith('-') || t === '-') {
      operands.push(t)
      continue
    }
    if (numFlag && /^-\d+$/.test(t)) continue
    const eq = t.indexOf('=')
    const name = eq === -1 ? t : t.slice(0, eq)
    const attached = eq === -1 ? undefined : t.slice(eq + 1)
    if (!has(spec, name)) return null
    if (spec[name] === 'b') {
      if (attached !== undefined) return null
      flags.set(name, true)
    } else if (spec[name] === 'a') {
      if (attached === undefined) return null
      flags.set(name, attached)
    } else if (spec[name] === 'o') {
      flags.set(name, attached ?? true)
    } else {
      const v = attached ?? args[++i]
      if (v === undefined || (attached === undefined && v.startsWith('-') && v !== '-')) return null
      flags.set(name, v)
    }
  }
  return { operands, flags, rest: [] }
}

// ---- stdin-only filters --------------------------------------------------

const FILTER_FLAGS: Record<string, { plain: string[]; num: string[] }> = {
  head: { plain: [], num: ['-n', '-c'] },
  tail: { plain: [], num: ['-n', '-c'] },
  wc: { plain: ['-l', '-w', '-c', '-m'], num: [] },
  sort: { plain: ['-n', '-r', '-u', '-h'], num: [] },
  uniq: { plain: ['-c', '-d', '-u'], num: [] },
  cut: { plain: [], num: [] },
  tr: { plain: ['-d', '-s'], num: [] },
  grep: { plain: ['-i', '-v', '-c', '-n', '-E', '-F', '-o', '-w', '-x', '-q'], num: ['-m', '-A', '-B', '-C'] },
  jq: { plain: ['-r', '-c', '-e', '-s', '-S', '-M'], num: [] },
}
const JQ_BAD = /\benv\b|\$ENV|input_filename|\binputs?\b|\bimport\b|\binclude\b|\bstderr\b|\bdebug\b|\$__prog|\$__loc__|\bhalt_error\b/

function sinkOk(argv: string[], piped: boolean): boolean {
  if (!has(FILTER_FLAGS, argv[0]) || !piped) return false // a sink reads stdin, so something must feed it
  const spec = FILTER_FLAGS[argv[0]]
  let operands = 0
  const args = argv.slice(1)
  for (let i = 0; i < args.length; i++) {
    const t = args[i]
    if (/^-\d+$/.test(t)) continue // head -5
    if (spec.plain.includes(t)) continue
    if (spec.num.includes(t)) {
      if (!isNum(args[i + 1] ?? '')) return false
      i++
      continue
    }
    if (argv[0] === 'cut' && /^-[df]./.test(t)) continue // -d: -f1
    if (argv[0] === 'cut' && (t === '-d' || t === '-f')) {
      if ((args[++i] ?? '').length === 0 || args[i].length > 3) return false
      continue
    }
    if (t.startsWith('-') && t !== '-') return false // unknown flag: defer
    operands++
    if (argv[0] === 'jq' && JQ_BAD.test(t)) return false
    if ((argv[0] === 'grep' || argv[0] === 'jq') && operands === 1) continue
    if (argv[0] === 'tr' && operands <= 2) continue
    return false // a file operand, or too many
  }
  if (argv[0] === 'jq' || argv[0] === 'grep') return operands === 1
  if (argv[0] === 'tr') return operands === 2 || (operands === 1 && args.includes('-d'))
  return true
}

// ---- git -----------------------------------------------------------------

// read-only subcommands: exact flag tables, pathspecs/revisions checked by pathOk
const GIT_READ: Record<string, Flags> = {
  status: { '-s': 'b', '-b': 'b', '--short': 'b', '--branch': 'b', '--porcelain': 'b', '--ignored': 'b' },
  log: { '--oneline': 'b', '--graph': 'b', '--decorate': 'b', '--stat': 'b', '--name-only': 'b', '--name-status': 'b', '-p': 'b', '--no-merges': 'b', '--first-parent': 'b', '--all': 'b', '-n': 'v', '--max-count': 'v', '--format': 'a', '--pretty': 'o', '--since': 'v', '--until': 'v', '--author': 'v', '--grep': 'v' },
  diff: { '--stat': 'b', '--name-only': 'b', '--name-status': 'b', '--cached': 'b', '--staged': 'b', '-w': 'b', '--shortstat': 'b', '--check': 'b', '--exit-code': 'b', '-p': 'b', '--summary': 'b', '--unified': 'a' },
  show: { '--stat': 'b', '--name-only': 'b', '--name-status': 'b', '--oneline': 'b', '-s': 'b', '--no-patch': 'b', '--format': 'a', '--pretty': 'o' },
  'rev-parse': { '--show-toplevel': 'b', '--abbrev-ref': 'b', '--short': 'b', '--verify': 'b', '-q': 'b', '--is-inside-work-tree': 'b' },
  'merge-base': { '--is-ancestor': 'b', '--fork-point': 'b' },
  'ls-files': { '-s': 'b', '--stage': 'b', '-m': 'b', '-o': 'b', '--others': 'b', '--exclude-standard': 'b', '-c': 'b' },
  blame: { '-w': 'b', '-L': 'v', '--porcelain': 'b' },
  describe: { '--tags': 'b', '--always': 'b', '--abbrev': 'o' },
  'cat-file': { '-t': 'b', '-p': 'b', '-s': 'b', '-e': 'b' },
  'rev-list': { '--count': 'b', '--parents': 'b', '--no-merges': 'b', '--first-parent': 'b', '-n': 'v', '--max-count': 'v', '--all': 'b' },
  'for-each-ref': { '--format': 'a', '--sort': 'a', '--count': 'a', '--merged': 'a', '--contains': 'a' },
  shortlog: { '-s': 'b', '-n': 'b', '-e': 'b' },
  grep: { '-n': 'b', '-i': 'b', '-l': 'b', '-c': 'b', '-w': 'b', '-F': 'b', '-I': 'b', '-h': 'b', '-H': 'b', '-q': 'b', '--count': 'b', '--no-color': 'b', '-e': 'v' },
}
const BRANCH_LIST: Flags = { '-a': 'b', '-r': 'b', '-v': 'b', '-vv': 'b', '--list': 'b', '--show-current': 'b', '--merged': 'a', '--no-merged': 'a' }
const BRANCH_NAME = /^[\w][\w./-]*$/
const TAGLIKE = /^(refs\/)?tags\/|^v/

async function git(argv: string[], env: Env, cfg: Cfg, segCount: number, cdSeen: boolean): Promise<R> {
  let i = 1
  let dir: string | null = null
  while (i < argv.length && argv[i].startsWith('-')) {
    if (argv[i] === '--no-pager') {
      i++
      continue
    }
    if (argv[i] !== '-C' || dir !== null || !underRoot(argv[i + 1] ?? '', cfg)) return 'ask' // -c, --exec-path, --git-dir ...
    dir = argv[i + 1]
    i += 2
  }
  const sub = argv[i]
  const args = argv.slice(i + 1)
  const numFlag = sub === 'log' || sub === 'rev-list'

  if (sub === 'diff') {
    // outside a work tree `git diff a b` compares two arbitrary files: it must run inside a repo,
    // in the directory we can check (so not after a `cd`)
    if ((dir === null && cdSeen) || !(await env.inRepo(dir ?? (await env.cwd())))) return 'ask'
    if (args.some(t => t.startsWith('/'))) return 'ask' // with two paths git may switch to --no-index and read any file
  }
  if (has(GIT_READ, sub)) {
    const s = scan(args, GIT_READ[sub], numFlag)
    return s && s.operands.every(o => pathOk(o, cfg)) && s.rest.every(o => pathOk(o, cfg)) ? ok('git ' + sub) : 'ask'
  }

  if (sub === 'fetch') {
    const s = scan(args, { '-p': 'b', '--prune': 'b', '--tags': 'b', '-q': 'b', '--quiet': 'b', '--no-tags': 'b' })
    return s && s.rest.length === 0 && s.operands.length <= 4 && (s.operands.length === 0 || cfg.remotes.has(s.operands[0])) && s.operands.slice(1).every(o => BRANCH_NAME.test(o)) ? ok('git fetch') : 'ask'
  }
  if (sub === 'pull') {
    const s = scan(args, { '--ff-only': 'b', '-q': 'b' })
    return s && s.rest.length === 0 && s.flags.has('--ff-only') && s.operands.length <= 2 && (s.operands.length === 0 || cfg.remotes.has(s.operands[0])) && s.operands.slice(1).every(o => BRANCH_NAME.test(o)) ? ok('git pull --ff-only') : 'ask'
  }
  if (sub === 'ls-remote') {
    const s = scan(args, { '--heads': 'b', '--tags': 'b' })
    return s && s.rest.length === 0 && s.operands.length >= 1 && cfg.remotes.has(s.operands[0]) ? ok('git ls-remote') : 'ask'
  }
  if (sub === 'remote') {
    const ok1 = args.length === 0 || (args.length === 1 && args[0] === '-v') || ((args[0] === 'show' || args[0] === 'get-url') && args.length === 2 && cfg.remotes.has(args[1]))
    return ok1 ? ok('git remote (read)') : 'ask'
  }
  if (sub === 'tag') return args.length === 0 || (args.length === 1 && (args[0] === '-l' || args[0] === '--list')) ? ok('git tag (list)') : 'ask'
  if (sub === 'branch') {
    const l = scan(args, BRANCH_LIST)
    if (l && l.rest.length === 0 && l.operands.length === 0) return ok('git branch (list)')
    const c = scan(args, {})
    return c && c.rest.length === 0 && c.flags.size === 0 && c.operands.length >= 1 && c.operands.length <= 2 && c.operands.every(o => BRANCH_NAME.test(o)) ? ok('git branch create') : 'ask'
  }
  if (sub === 'switch') {
    const s = scan(args, { '-c': 'v' })
    if (!s || s.rest.length) return 'ask'
    return (s.flags.size === 0 && s.operands.length === 1 && BRANCH_NAME.test(s.operands[0])) || (s.flags.size === 1 && s.operands.length <= 1 && BRANCH_NAME.test(String(s.flags.get('-c')))) ? ok('git switch') : 'ask'
  }
  if (sub === 'checkout') {
    const s = scan(args, { '-b': 'v' }) // a plain `checkout <x>` can overwrite files when x is a path: use switch
    return s && s.rest.length === 0 && s.flags.size === 1 && s.operands.length <= 1 && BRANCH_NAME.test(String(s.flags.get('-b'))) ? ok('git checkout -b') : 'ask'
  }
  if (sub === 'add') {
    const s = scan(args, { '-A': 'b', '--all': 'b', '-u': 'b', '--update': 'b', '-N': 'b', '--intent-to-add': 'b', '-n': 'b', '--dry-run': 'b', '-v': 'b' })
    return s && s.operands.every(o => pathOk(o, cfg)) && s.rest.every(o => pathOk(o, cfg)) ? ok('git add') : 'ask'
  }
  if (sub === 'commit') {
    const s = scan(args, { '-m': 'v', '-am': 'v', '-a': 'b', '--amend': 'b', '--no-edit': 'b', '--allow-empty': 'b', '-q': 'b', '-s': 'b', '--signoff': 'b' })
    return s && s.rest.length === 0 && s.operands.length === 0 ? ok('git commit') : 'ask'
  }
  if (sub === 'cherry-pick') {
    const s = scan(args, { '-x': 'b', '-n': 'b', '--no-commit': 'b', '--continue': 'b', '--abort': 'b', '--skip': 'b', '-m': 'v' })
    return s && s.rest.length === 0 && s.operands.every(o => OPERAND.test(o)) ? ok('git cherry-pick') : 'ask'
  }
  if (sub === 'merge') {
    const s = scan(args, { '--no-ff': 'b', '--ff-only': 'b', '--ff': 'b', '--no-commit': 'b', '--squash': 'b', '--abort': 'b', '--continue': 'b', '--no-edit': 'b', '-q': 'b', '-m': 'v' })
    return s && s.rest.length === 0 && s.operands.every(o => OPERAND.test(o)) ? ok('git merge') : 'ask'
  }
  if (sub === 'rebase') {
    const s = scan(args, { '--onto': 'v', '--continue': 'b', '--abort': 'b', '--skip': 'b', '--autosquash': 'b', '--no-autosquash': 'b', '-q': 'b' })
    return s && s.rest.length === 0 && s.operands.every(o => OPERAND.test(o)) ? ok('git rebase') : 'ask'
  }
  if (sub === 'reset') {
    const s = scan(args, { '--soft': 'b', '--mixed': 'b', '-q': 'b' })
    return s && s.operands.every(o => pathOk(o, cfg)) && s.rest.every(o => pathOk(o, cfg)) ? ok('git reset (soft/mixed)') : 'ask'
  }
  if (sub === 'worktree') {
    if (args[0] === 'list' && args.length === 1) return ok('git worktree list')
    if (args[0] === 'prune' && args.length === 1) return ok('git worktree prune')
    // relative paths are resolved against -C or the session cwd, which a `cd` before this segment would change
    const base = dir ?? (await env.cwd())
    const resolve = (p: string) => (p.startsWith('/') ? p : normPath(base + '/' + p)) // base is a real path, so textual `..` is right here
    const pathFine = (p: string) => (p.startsWith('/') || !cdSeen) && underRoot(resolve(p), cfg)
    if (args[0] === 'remove' && args.length === 2) return pathFine(args[1]) ? ok('git worktree remove') : 'ask'
    if (args[0] === 'add') {
      const s = scan(args.slice(1), { '-b': 'v', '--detach': 'b' })
      const b = s?.flags.get('-b')
      return s && s.rest.length === 0 && s.operands.length >= 1 && s.operands.length <= 2 && pathFine(s.operands[0]) && s.operands.slice(1).every(o => BRANCH_NAME.test(o)) && (b === undefined || (typeof b === 'string' && BRANCH_NAME.test(b))) ? ok('git worktree add') : 'ask'
    }
    return 'ask'
  }
  if (sub !== 'push') return 'ask'

  // ---- push ----
  const s = scan(args, { '-u': 'b', '--set-upstream': 'b', '--tags': 'b' })
  if (!s || s.rest.length) return 'ask'
  const [remote, ...refs] = s.operands
  if (!cfg.remotes.has(remote)) return 'ask'
  if (s.flags.has('--tags')) return refs.length === 0 ? prodGate(env, 'tags') : 'ask'
  if (refs.length === 0) return 'ask'
  const mainDst = new RegExp('^(refs/heads/)?' + cfg.mainBranch.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '$')
  let needGate = false
  let prod = false
  for (const r of refs) {
    if (/[*?[\]{}~^@]/.test(r) || r.startsWith('+') || r.startsWith(':') || r === 'HEAD') return 'ask' // glob, force, delete, bare HEAD
    const parts = r.split(':')
    if (parts.length > 2) return 'ask'
    const src = parts[0]
    const dst = parts[1] ?? src
    if (mainDst.test(dst)) {
      if (src !== 'HEAD') return 'ask' // the gate is about HEAD, so the source must be HEAD
      needGate = true
    } else if (TAGLIKE.test(dst)) {
      prod = true
    } else if ((dst.startsWith('refs/') && !dst.startsWith('refs/heads/')) || dst.startsWith('heads/') || dst.includes('//')) {
      return 'ask' // other namespaces and DWIM spellings (heads/main) are not ours to judge
    } else if (/^(refs\/heads\/)?(master|HEAD)$/.test(dst)) {
      return 'ask'
    }
  }
  if (prod && !(await env.prodOpen())) return 'ask'
  if (needGate) {
    // The session cwd is not reliable (a subagent's shell may sit elsewhere), so the push must name
    // the repo itself: `git -C <merge worktree> push origin HEAD:main`, as the only segment.
    if (dir === null || segCount !== 1 || refs.length !== 1 || !cfg.mergeWorktree.test(dir)) return 'ask'
    const head = await env.head(dir)
    const m = /^Merge (?:T-|branch 'task\/T-)(\d+)\b/.exec(head.subject)
    if (!m || head.parents !== 2) return 'ask' // a real merge commit, not a hand-written subject
    if (!(await env.mergeMatches(dir, 'T-' + m[1]))) return 'ask' // exactly the reviewed branch merged into the current main
    if (!(await env.boardGate('T-' + m[1], dir))) return 'ask'
    return ok('git push ' + cfg.mainBranch + ' (board gate passed)')
  }
  return ok('git push')
}

async function prodGate(env: Env, what: string): Promise<R> {
  return (await env.prodOpen()) ? ok('prod gate open: ' + what) : 'ask'
}

// ---- kubectl -------------------------------------------------------------
//
// Strict grammar: `kubectl [--context X] [-n NS] VERB ...`. Before the verb only --context and -n are
// understood, so an unknown flag can never shift which word is the verb. After the verb, each verb has a
// fixed flag table. Tokens after `--` are the program run in the pod and are not kubectl's.

const NS: Flags = { '-n': 'v', '--namespace': 'v' }
const SEL: Flags = { '-l': 'v', '--selector': 'v' }
const ALLNS: Flags = { '-A': 'b', '--all-namespaces': 'b' }
const KUBE: Record<string, Flags> = {
  get: { ...NS, ...SEL, ...ALLNS, '-o': 'v', '--output': 'v', '--field-selector': 'v', '--show-labels': 'b', '--sort-by': 'v', '--no-headers': 'b' },
  describe: { ...NS, ...SEL, ...ALLNS },
  logs: { ...NS, ...SEL, '-c': 'v', '--container': 'v', '--tail': 'v', '--since': 'v', '--previous': 'b', '-p': 'b', '--timestamps': 'b' },
  top: { ...NS, ...SEL, ...ALLNS, '--containers': 'b' },
  events: { ...NS, ...ALLNS, '--for': 'v' },
  rollout: { ...NS, '--timeout': 'v', '--revision': 'v' },
  patch: { ...NS, '-p': 'v', '--patch': 'v', '--type': 'v' },
  set: { ...NS, '-c': 'v', '--containers': 'v', '--list': 'b' },
  scale: { ...NS, '--replicas': 'v' },
  annotate: { ...NS, '--overwrite': 'b' },
  label: { ...NS, '--overwrite': 'b' },
  exec: { ...NS, '-c': 'v', '--container': 'v', '-i': 'b', '-t': 'b', '-it': 'b', '-ti': 'b', '--stdin': 'b', '--tty': 'b' },
  'port-forward': { ...NS },
  wait: { ...NS, ...SEL, '--for': 'v', '--timeout': 'v' },
  delete: { ...NS, ...SEL, '--grace-period': 'v', '--wait': 'a' }, // --wait is a switch in kubectl: only `--wait=false`
}
const KUBE_READ = new Set(['get', 'describe', 'logs', 'top', 'events'])
const ROLLOUT_SUB = new Set(['status', 'history', 'restart', 'pause', 'resume', 'undo'])
// Writes only touch an allowlist of workload types (never RBAC, namespaces, nodes, crds, webhooks ...).
const WRITABLE = new Set(['deploy', 'deployment', 'deployments', 'sts', 'statefulset', 'statefulsets', 'ds', 'daemonset', 'daemonsets', 'cronjob', 'cronjobs', 'cj', 'job', 'jobs', 'pod', 'pods', 'po', 'rs', 'replicaset', 'replicasets', 'cm', 'configmap', 'configmaps', 'svc', 'service', 'services', 'ing', 'ingress', 'ingresses'])
const DELETABLE = new Set(['pod', 'pods', 'po', 'job', 'jobs', 'rs', 'replicaset', 'replicasets'])
// `deployments.apps` -> `deployments`; `deploy,sts` -> both
const typeOf = (t: string) => t.toLowerCase().split('/')[0].split(',').map(x => x.split('.')[0])
// `type name1 name2` or `type/name type/name`: every type must be allowed, names must not hide a type
function targetsIn(all: string[], allowed: Set<string>): boolean {
  // `key=value` pairs (label, annotate, set image/env) are not targets
  const targets = all.filter(t => !/^[\w./-]+=/.test(t) && !/^[\w./]+-$/.test(t))
  if (targets.length === 0) return false
  if (targets[0].includes('/')) return targets.every(t => t.includes('/') && typeOf(t).every(x => allowed.has(x)))
  return typeOf(targets[0]).every(x => allowed.has(x)) && targets.slice(1).every(t => !t.includes('/'))
}
const WRITE_VERBS = new Set(['patch', 'label', 'annotate', 'scale', 'set', 'delete', 'exec', 'rollout'])

function kubectl(argv: string[], cfg: Cfg): R {
  const dd = argv.indexOf('--')
  const own = dd === -1 ? argv.slice(1) : argv.slice(1, dd)
  const podCmd = dd !== -1

  // global part: only --context and -n/--namespace, then the verb
  let ctx = ''
  let ctxCount = 0
  let gns = ''
  let i = 0
  for (; i < own.length && own[i].startsWith('-'); i++) {
    const t = own[i]
    if (t === '--context') {
      ctx = own[++i] ?? ''
      ctxCount++
    } else if (t.startsWith('--context=')) {
      ctx = t.slice(10)
      ctxCount++
    } else if (t === '-n' || t === '--namespace') {
      if (own[++i] === undefined) return 'ask'
      gns = own[i]
    } else if (t.startsWith('--namespace=')) {
      gns = t.slice(12)
    } else return 'ask' // any other flag before the verb: do not guess
  }
  if (ctx === '' || ctxCount !== 1 || i >= own.length) return 'ask' // implicit or repeated context, no verb
  const verb = own[i]
  if (!has(KUBE, verb)) return 'ask' // apply, create, replace, edit, cp, debug, run, proxy, plugins ...
  const s = scan(own.slice(i + 1), KUBE[verb])
  if (!s || s.rest.length > 0) return 'ask'
  const { operands, flags } = s
  if (!operands.every(o => OPERAND.test(o))) return 'ask'
  if (podCmd && verb !== 'exec') return 'ask'
  if (verb === 'exec' && (!podCmd || operands.length !== 1)) return 'ask'

  const secretish = [...operands, ...[...flags.values()].filter((v): v is string => typeof v === 'string')].some(t => /secret/i.test(t))
  const hasOutput = flags.has('-o') || flags.has('--output')
  if (KUBE_READ.has(verb) && secretish && hasOutput) return 'ask'

  let sub = ''
  let targets = operands
  if (verb === 'rollout') {
    sub = operands[0] ?? ''
    if (!ROLLOUT_SUB.has(sub)) return 'ask'
    targets = operands.slice(1)
  }
  if (verb === 'set') {
    if (!['image', 'env', 'resources'].includes(operands[0] ?? '') || secretish) return 'ask'
    targets = operands.slice(1)
  }
  if (['patch', 'label', 'annotate', 'scale', 'set'].includes(verb)) {
    // `patch secret` stays possible on dev (deliberate); every target must be an allowed workload type
    const allowed = verb === 'patch' ? new Set([...WRITABLE, 'secret', 'secrets']) : WRITABLE
    if (!targetsIn(targets, allowed)) return 'ask'
  }
  if (verb === 'delete' && !targetsIn(targets, DELETABLE)) return 'ask'

  if (flags.has('-n') && flags.has('--namespace')) return 'ask' // one flag in kubectl: the spelling that wins is unclear
  const ns = (flags.get('-n') ?? flags.get('--namespace') ?? gns) as string
  const mutates = WRITE_VERBS.has(verb) && !(verb === 'rollout' && ['status', 'history'].includes(sub))
  if (ctx === cfg.devContext && mutates && !cfg.devWriteNamespaces.has(ns)) return 'ask' // writes and exec only in the named dev namespaces

  if (ctx !== cfg.devContext) {
    const readOnly = KUBE_READ.has(verb) || (verb === 'rollout' && ['status', 'history', 'restart'].includes(sub))
    return readOnly && !secretish ? 'prod' : 'ask'
  }
  return ok('kubectl dev')
}

// ---- other programs ------------------------------------------------------

const BOARD_TASK = new Set(['show', 'list', 'search', 'next', 'claim', 'progress', 'blocked', 'worktree', 'pr', 'estimate', 'merging', 'merged', 'cleanup', 'done', 'create', 'release', 'deploy', 'comment'])
// `register`, `start`, `review` and `export BOARD_*` change who the board thinks you are, which the merge gate trusts: not ours.
const BOARD_TOP = new Set(['status', 'inbox', 'heartbeat', 'finished', 'gate', 'help', 'ask'])

function board(argv: string[]): R {
  if (argv.some(t => /prod/i.test(t) || /^--human/.test(t))) return 'ask' // prod deploy goes through the prod gate, human flags are not ours
  const [, a, b] = argv
  if (BOARD_TOP.has(a)) return ok('board ' + a)
  if (a === 'task' && BOARD_TASK.has(b)) return ok('board task ' + b)
  return 'ask' // patch, grant, answer, role, ceiling, register, review ...
}

const TEA_PR = new Set(['list', 'ls', 'create', 'c', 'checkout', 'co'])
const TEA_ISSUE = new Set(['list', 'ls', 'create', 'c'])

function tea(argv: string[]): R {
  // --labels would let a PR be auto-merged by the forge, past the board gate
  if (argv.some(t => t === '--login' || t === '-l' || t.startsWith('--login=') || t === '--labels' || t === '-L' || t.startsWith('--labels='))) return 'ask'
  const [, a, b] = argv
  if (a === 'whoami') return ok('tea whoami')
  if (a === 'comment') return ok('tea comment')
  if ((a === 'pr' || a === 'pulls' || a === 'pull') && (b === undefined || isNum(b) || TEA_PR.has(b))) return ok('tea pr')
  if ((a === 'issues' || a === 'issue') && (b === undefined || isNum(b) || TEA_ISSUE.has(b))) return ok('tea issues')
  return 'ask' // merge, m, approve, lgtm, clean, close, edit ...
}

function plain(argv: string[], cfg: Cfg): R {
  const [p, ...a] = argv
  if (p === 'echo' || p === 'pwd' || p === 'true') return ok(p)
  if (p === 'ls') return a.every(t => (t.startsWith('-') ? /^-[lahR1tS]+$/.test(t) : (t.startsWith('/') ? underRoot(t, cfg) : pathOk(t, cfg)))) ? ok('ls') : 'ask'
  return 'ask'
}

// ---- decide --------------------------------------------------------------

export async function decide(cmd: string, env: Env, cfg: Cfg = makeCfg()): Promise<Verdict> {
  const segs = parse(cmd)
  if (!segs) return null
  if (SECRET_WORDS.test(cmd) || SECRET_WORDS.test(segs.map(s => s.argv.join(' ')).join(' '))) return null
  const why: string[] = []
  let cdSeen = false
  for (const { argv: raw, piped } of segs) {
    // the RTK hook rewrites `git status` to `rtk git status`; look through it, but never `rtk proxy`
    const s = raw[0] === 'rtk' && ['git', 'kubectl', 'tea', 'board'].includes(raw[1]) ? raw.slice(1) : raw
    const p = s[0]
    let v: R
    if (/^\w+=/.test(p)) return null // env-var prefix
    if (p === 'echo' || p === 'pwd' || p === 'true' || p === 'ls') v = plain(s, cfg)
    else if (has(FILTER_FLAGS, p)) v = sinkOk(s, piped) ? ok('filter') : 'ask'
    else if (p === 'cd') {
      v = s.length === 2 && underRoot(s[1], cfg) ? ok('cd') : 'ask'
      cdSeen = true
    }
    else if (p === 'board') v = board(s)
    else if (p === 'tea') v = tea(s)
    else if (p === 'kubectl') v = kubectl(s, cfg)
    else if (p === 'git') v = await git(s, env, cfg, segs.length, cdSeen)
    else return null
    if (v === 'ask') return null
    if (v === 'prod') {
      if (!(await env.prodOpen())) return null
      v = ok('prod gate open')
    }
    if (v === null) return null
    why.push(v.reason.replace('next-autonomy: ', ''))
  }
  return ok(why.join(', '))
}

// ---- hook ----------------------------------------------------------------

export function gateOk(exitCode: number, stdout: string): boolean {
  if (exitCode !== 0) return false
  try {
    return JSON.parse(stdout).ok === true // a live answer, not just an exit code
  } catch {
    return false
  }
}

export const register: Register = (on, options) => {
  const cfg = makeCfg(options)
  on('tool.check', { tool: 'Bash' }, async ($, e, next) => {
    const base = await next(e)
    if (base.decision === 'deny') return base // a deny rule always wins
    if (base.decision === 'ask' && (base.rule !== undefined || base.hook !== undefined)) return base // so does an explicit ask rule or hook
    const input = e.input as { command?: string; dangerouslyDisableSandbox?: boolean }
    if (typeof input.command !== 'string' || input.dangerouslyDisableSandbox) return base
    const run = (argv: string[], cwd?: string) => $.process.run(argv, { cwd, timeoutMs: TIMEOUT_MS })
    const env: Env = {
      cwd: () => $.session.cwd(),
      head: async dir => {
        const subject = (await run(['git', 'log', '-1', '--format=%s'], dir)).stdout.trim()
        const parents = (await run(['git', 'rev-list', '--parents', '-n', '1', 'HEAD'], dir)).stdout.trim().split(/\s+/).length - 1
        return { subject, parents }
      },
      mergeMatches: async (dir, t) => {
        const out = async (argv: string[]) => {
          const r = await run(argv, dir)
          return r.exitCode === 0 ? r.stdout.trim() : ''
        }
        const [, p1, p2] = (await out(['git', 'rev-list', '--parents', '-n', '1', 'HEAD'])).split(/\s+/)
        const base = await out(['git', 'rev-parse', `${cfg.mainRemote}/${cfg.mainBranch}`])
        const tip = await out(['git', 'rev-parse', `${cfg.mainRemote}/task/${t}`])
        const tree = await out(['git', 'rev-parse', 'HEAD^{tree}'])
        const merged = (await out(['git', 'merge-tree', '--write-tree', p1 ?? '', p2 ?? ''])).split('\n')[0]
        return !!p1 && !!p2 && p1 === base && p2 === tip && tree !== '' && tree === merged
      },
      boardGate: async (t, dir) => {
        const r = await run(['board', 'gate', 'merge', t], dir)
        return gateOk(r.exitCode, r.stdout)
      },
      inRepo: async dir => (await run(['git', 'rev-parse', '--is-inside-work-tree'], dir)).stdout.trim() === 'true',
      prodOpen: async () => {
        try {
          return (await $.fs.read(cfg.prodGateFile)).trim().split('\n')[0].trim() === 'open'
        } catch {
          return false
        }
      },
    }
    try {
      return (await decide(input.command, env, cfg)) ?? base
    } catch {
      return base // a failing or hanging check never approves
    }
  })
}
