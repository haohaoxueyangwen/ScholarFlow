# ScholarFlow

ScholarFlow is a research-assistant project built around the Pi agent runtime.
Pi handles model interaction and sessions; ScholarFlow provides research tools,
evidence-linked notes, project-scoped knowledge, and reviewable write operations.

> **Early source preview:** this repository is a selectively prepared preview,
> not a complete or production-ready release. A clean-checkout installation and
> the full research workflow have not yet been qualified.

The intended research workflow is: select a project and a PDF, discuss selected
pages with Pi, review a proposed note, and search approved notes with links back
to their sources. Model and knowledge services require separate configuration;
neither a local model nor an external API is included in this repository.

## Scope and limitations

- This snapshot includes the Pi harness, selected capabilities, the reading-desk
  source, and the project-scoped knowledge code. The public snapshot uses
  example identifiers and contains no user's papers, notes, configuration,
  runtime artifacts, model weights, or existing private Git history.
- Historical personal corpus paths and project-specific scripts are excluded.
  The experimental CLI can register explicitly selected PDF/Markdown source
  directories to an existing project, with optional discovery on agent start.
  Discovery does not itself read with a model or index any source. The legacy
  library launcher in this preview only permits its local embedding action.
- The optional Nature writing skill can be installed locally from the pinned
  upstream Git revision, checked against a reviewed content hash and explicitly
  activated. Upstream skill files are not redistributed in this snapshot; their
  own Apache-2.0 license and third-party materials remain applicable.
- Windows is the current primary target. Python 3.11+, Node.js 22.19+, the
  project dependencies, a configured Pi provider, and separately provisioned
  knowledge services are required. Building has been checked in isolation with
  previously installed dependencies; installation from a clean clone has not.
- No project-level license has been selected. Public visibility alone does not
  grant an open-source license. Third-party components retain their own terms.

For the actively maintained project, Pi remains the only model/session runtime;
the web interface is an optional client, not a replacement agent runtime.

## Experimental Windows CLI

From a clean source snapshot with Python 3.11+ and Node.js 22.19+ installed,
`powershell -NoProfile -File .\scripts\install-scholarflow.ps1 -WithWeb -WithKnowledge`
installs pinned Pi dependencies, the optional browser client and LightRAG, and
a separate user-local Python environment. The Electron binary, model providers,
and the embedding server/model are **not** included. The installer uses npm
11.6.2 to match the reviewed lockfile. It does not modify the system PATH; run
the printed `scholarflow.cmd` path. Existing dependency directories are not
removed. This installer remains an experimental preview.

Run `scholarflow doctor`, `scholarflow projects --help`, `scholarflow sources
--help`, or `scholarflow nature --help`. Nature installation takes either an
existing upstream checkout (`nature install --checkout PATH`) or an explicit
network fetch (`nature install --fetch`); activate the returned version with
`nature activate --version VERSION`. Active content is revalidated before Pi
loads it. Third-party skill instructions are not a security sandbox.
`nature check` compares the pinned commit with upstream without changing the
active version. A newer commit requires a new reviewed pin and content hash;
there is no unattended upstream update in this preview.

For example, in PowerShell after installation:

```powershell
$sf = Join-Path $env:LOCALAPPDATA 'ScholarFlow\bin\scholarflow.cmd'
& $sf doctor
& $sf auth set-deepseek  # Prompts privately; do not paste a key into chat.
& $sf projects preview --name 'Example project' --title 'Reading' --goal 'Review a paper'
# Review the preview, then supply its exact request_id and proposal_hash:
& $sf projects commit --request-id '<request_id>' --reviewed-hash '<proposal_hash>'
& $sf workspaces prepare --project '<stage_id>' --provider deepseek --pdf '.\paper.pdf' --pages '1'
& $sf pi --workspace '<workspace path returned above>'
# Or, if installed with -WithWeb, start the optional local browser client:
& $sf web
```

The agent does not invent provider credentials. `auth set-deepseek` stores the
key in a **plaintext Pi auth file** under the current user's data home; protect
that folder and do not copy it into a repository. Alternatively set
`DEEPSEEK_API_KEY` in the launching environment; existing Pi credentials also
remain supported. `auth status` never prints the key or makes a model request.
Consent before transmitting material to an external API. Sessions,
project facts and the source registry are stored under the user data home,
separately from the downloaded source package. Moving to a second PC does not
automatically copy your research data or credentials. An existing library and
workspace directory can be selected explicitly with the global `--library-home`
and `--workspace-home` options; neither is migrated or overwritten by setup.

`sources add` registers a directory for one existing project; `--auto` opts that
directory into discovery when starting the agent. Discovery records hashes and
metadata only. It does not read papers with a model, approve notes, or silently
add documents to the AI knowledge index. Before semantic indexing, separately
provision the reviewed Qwen3 embedding model and a loopback service, then use
`scholarflow knowledge configure-embedding --help`. The configured SHA-256 is
an operator assertion: this preview does not remotely attest the running
server's model identity. The tool never starts or selects an unknown service.
