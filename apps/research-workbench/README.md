# ScholarFlow Research Workbench

Local-first React workbench for ScholarFlow. It renders research-domain state
from the loopback Application API and never owns an independent agent loop or
research persistence layer.

## Development

From the repository root, start the complete local stack with:

```powershell
.\scripts\workbench.ps1
```

For frontend-only work:

```powershell
npm install --prefix apps\research-workbench
npm run dev --prefix apps\research-workbench
```

If the Application API is unavailable, the UI enters an explicit read-only
demo mode.

## Validation

```powershell
npm test --prefix apps\research-workbench
npm run lint --prefix apps\research-workbench
```

The deployment scaffolding is retained for build reproducibility, but this
private research interface is not published by default.
