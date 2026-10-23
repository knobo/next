---
theme: default
title: next — agenter uten tilsyn
info: |
  next: en utviklingsløkke for AI-agenter, og en tavle som gjør det trygt å la dem gå alene.
colorSchema: dark
transition: slide-left
mdc: true
---

# next

Agenter som jobber uten tilsyn<br>
— og hva som må være sant for at det er trygt

<div class="abs-bl m-6 text-sm opacity-60">Knut Olav Bøhmer · github.com/knobo/next</div>

---

# Utgangspunktet

Én agent i én økt trenger ingen koordinering. Det endrer seg straks noe av dette er sant:

<v-clicks>

- Agenten **dør midt i** en oppgave — krasj, kvote, lokket på laptopen igjen
- **To agenter** redigerer de samme filene
- En agent **reviewer sitt eget arbeid**
- En agent **gir seg selv rettigheter**
- Kvoten er **delt** mellom økter og maskiner
- Et menneske må bestemme noe **kl. 03**

</v-clicks>

<div v-click class="mt-8 text-xl">
Løkka er ikke det interessante. <b>Begrensningene</b> er det.
</div>

---

# To deler som bare gir mening sammen

<div class="grid grid-cols-2 gap-10 mt-8">
<div>

### `/next` — skillen

En full utviklingsløkke:

oppgave → worktree → subagent → review → gate → merge → deploy → verifiser → rydd opp

**Stopper aldri for å spørre.**

</div>
<div>

### board — tavla

En liten tjeneste som holder

- køen, rollene, kvotetakene
- en append-only hendelseslogg
- hvem som eier hva

**Ingen intelligens.** Den svarer på faktaspørsmål.

</div>
</div>

---

# Arkitektur

```mermaid {scale: 0.62}
flowchart LR
    H["Menneske<br/>telefon eller laptop"]
    subgraph board["Tavla — én tjeneste, én SQLite-fil"]
        Q["kø<br/>oppgaver · spørsmål · roller"]
        P["policy<br/>rettigheter · kvotetak"]
        L["append-only<br/>hendelseslogg"]
    end
    A1["/next koordinator"]
    A2["/next implementer"]
    A3["/next reviewer"]
    F["forge<br/>GitHub / Forgejo"]
    A1 <-->|"board CLI<br/>HTTP + bearer"| Q
    A2 <--> Q
    A3 <--> Q
    P --> Q
    Q --> L
    Q -->|"push: spørsmål,<br/>blokkeringer"| H
    H -->|"svar, faser"| Q
    A1 -->|"PR, merge"| F
```

---

# Løkka

```mermaid {scale: 0.48}
sequenceDiagram
    participant C as Koordinator
    participant B as Tavla
    participant S as Subagent
    participant R as Reviewer
    C->>B: claim neste oppgave
    C->>C: worktree + branch
    C->>S: implementer (eksplisitt modell)
    S-->>C: ferdig + tokens brukt
    C->>R: review (aldri en fork)
    R->>B: review-resultat (åpne funn)
    C->>B: gate merge?
    B-->>C: ok / nei + grunner
    C->>C: merge, deploy, verifiser
    C->>B: done — rydd opp
```

---

# «Aldri stopp» — men stoppbar

`board ask` returnerer **med en gang**, med et standardsvar.

<v-clicks>

1. Agenten bygger standardvalget og merker PR-en
2. Går videre til neste oppgave
3. Mennesket svarer når det våkner
4. Tavla gjør et overstyrt svar om til **nytt arbeid**

</v-clicks>

<div v-click class="mt-8">

Det som venter på deg står på oppgavens eget kort: spørsmålene som blokkerer, og hver kommando du er ventet å kjøre — med kopiknapp.

</div>

---

# Prompt-tekst holder ikke

«Kjør en review før du merger» står i prompten. Det har feilet i praksis.

Tre mekaniske lag, billigst først:

| Lag | Hva det stopper |
|---|---|
| **Branch protection** på `main` | push rett til main, merge på rød CI |
| **`PreToolUse`-hook** | `gh pr merge` uten at tavla sier ja — også i subagenter |
| **Gate på tavla** | åpne funn, review satt av eieren selv, manglende `merge`-rettighet, to merger samtidig |

<div v-click class="mt-6 text-amber-300">
Nylig: hooken leste fra feil cache-katalog, fant ingen oppgave — og slapp alt gjennom. «Fails open» er et valg. Det må testes.
</div>

---

# Mennesket har sin egen nøkkel

- **Eget human-token.** Uten det er `by: human` en påstand enhver agent kan skrive
- Agenter kan ikke: gi seg selv rettigheter, flytte kvotetak, endre fase, pinne roller
- Siden sier alltid **hvem nettleseren er logget inn som**
- Lenke i push-varsel → token byttes straks mot `HttpOnly`-cookie, ren URL
- `board open --qr` — telefonen logges inn én gang

<div v-click class="mt-6">

Per-agent-tokens bundet til `harness@host`: et token for `codex@build-*` kan ikke registrere seg som `claude-code`.

</div>

---

# Roller

| Rolle | Hvordan |
|---|---|
| **koordinator** | singleton per prosjekt — valgt av agentene, eller pinnet av mennesket |
| implementer | tar oppgaver fra køen |
| reviewer | aldri samme agent som eieren |
| tester | krever `browser-test`-evnen |

En **lease** på hver oppgave. Utløper den, merker reaperen oppgaven `orphaned` — og neste agent tar over **med worktree, branch, PR og siste fremdriftsnotat**.

---

# Kvote — per konto

Tavla tar den **høyeste målingen per vindu** (5h, 7d) på tvers av alle økter, og svarer `.stop`.

<v-clicks>

- Agenten regner ikke selv på terskler
- Målingen **huskes** etter at agenten er borte — ellers brenner neste start kvoten på nytt
- **Flere kontoer:** to Claude-innlogginger er to kvoter
- `next-run claude-kunde` starter en økt på riktig konto
- `next-run --auto` tar første tillatte konto som ikke er over taket
- Claude, Codex og Antigravity side om side

</v-clicks>

---

# Workspaces — én database per kunde

Én tavle, men **ingen deling** mellom kunder og private prosjekter.

| Token | Rekkevidde |
|---|---|
| human-token | **admin** — alle workspaces |
| delt agent-token | bare `default` |
| agent-token | workspacen det ble utstedt i |
| owner-token | mennesket, i én workspace — demo, kunde |

<div class="mt-6">

En workspace er en **egen SQLite-fil**. En spørring som glemmer et filter kan ikke lekke — radene finnes ikke i filen.

Feil workspace = **403**, aldri stille fallback.

</div>

---

# Teknologi — kjedelig med vilje

- **Python stdlib**: `http.server` + `sqlite3`. Ingen avhengigheter
- **Én pod, én skriver.** Skalerer ikke horisontalt, og skal ikke
- Kildekoden deployes som ConfigMap — ingen image, intet registry
- **`conformance.sh` er spesifikasjonen**: 646 sjekker mot hele API-et, egen instans hver gang
- Endrer du oppførsel, endrer du sjekken i samme commit
- Kommentarene forklarer **hvorfor** — som regel fordi fraværet knakk noe

---

# Ærlig talt

Dette er et **eksperiment**, ikke et produkt.

<v-clicks>

- En agent kan logge en review den aldri kjørte. Gaten fanger den vanlige varianten — eier på eget arbeid — men ikke en bestemt løgner
- CI-status sjekkes ikke av gaten; branch protection er sikringen
- Claude Code brukes daglig. Codex, Grok og Antigravity er støttet, men mindre testet
- Et token i `pass` på en maskin der en agent kjører, er et token agenten kan lese

</v-clicks>

---
layout: center
class: text-center
---

# Spørsmål?

**github.com/knobo/next** · MIT

<div class="mt-6 opacity-70">
README · DESIGN.md · USAGE.md
</div>
