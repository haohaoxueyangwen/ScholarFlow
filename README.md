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
- Personal corpus discovery and historical, project-specific operation scripts
  are deliberately excluded or disabled. In this preview, the library launcher
  only permits its local embedding startup action; automatic discovery of a
  user's research folders is not provided.
- Nature Skills is represented by its upstream source and license metadata only;
  the upstream skill content and its installation/update workflow are not part
  of this preview. The manuscript adapter is therefore not fully available.
- Windows is the current primary target. Python 3.11+, Node.js 22.19+, the
  project dependencies, a configured Pi provider, and separately provisioned
  knowledge services are required. Building has been checked in isolation with
  previously installed dependencies; installation from a clean clone has not.
- No project-level license has been selected. Public visibility alone does not
  grant an open-source license. Third-party components retain their own terms.

For the actively maintained project, Pi remains the only model/session runtime;
the web interface is an optional client, not a replacement agent runtime.
