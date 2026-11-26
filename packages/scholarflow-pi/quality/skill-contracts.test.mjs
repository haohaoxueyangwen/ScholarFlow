import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { refreshSkillLock, validateSkillPackage } from "../scripts/validate-skills.mjs";

test("ScholarFlow Skills match the portable spec and reviewed source lock", async () => {
  const report = await validateSkillPackage();
  assert.equal(report.status, "pass", report.errors.join("\n"));
  assert.equal(report.skill_count, 13);
  assert.deepEqual(report.warnings, []);
});

test("Skill validation blocks unreviewed content drift", async () => {
  const root = await mkdtemp(join(tmpdir(), "scholarflow-skill-contract-"));
  try {
    await mkdir(join(root, "skills", "fixture-skill"), { recursive: true });
    await mkdir(join(root, "quality"), { recursive: true });
    await mkdir(join(root, "vendor", "nature-skills"), { recursive: true });
    await writeFile(join(root, "package.json"), JSON.stringify({ version: "1.0.0" }));
    await writeFile(
      join(root, "skills", "fixture-skill", "SKILL.md"),
      "---\nname: fixture-skill\ndescription: A portable fixture.\n---\n\n# Fixture\n",
    );
    await writeFile(
      join(root, "quality", "ecosystem-manifest.json"),
      JSON.stringify({ vendored: [] }),
    );
    await writeFile(
      join(root, "vendor", "nature-skills", "SOURCE.json"),
      JSON.stringify({ name: "nature-skills", source: "https://example.test", commit: "0".repeat(40) }),
    );
    await writeFile(
      join(root, "quality", "skill-lock.json"),
      JSON.stringify({
        schema_version: 1,
        spec_url: "https://agentskills.io/specification",
        package_version: "1.0.0",
        skills: [{
          name: "fixture-skill",
          source_type: "first_party",
          source: "workspace://packages/scholarflow-pi/skills/fixture-skill",
          source_revision: "@scholarflow/pi@1.0.0",
          content_sha256: "0".repeat(64),
          update_policy: "repository_versioned_and_release_gated",
          reviewed_at: "2026-08-28",
        }],
      }),
    );
    const report = await validateSkillPackage(root);
    assert.equal(report.status, "fail");
    assert.match(report.errors.join("\n"), /content hash differs/);
    await refreshSkillLock("fixture-skill", root);
    const reviewed = await validateSkillPackage(root);
    assert.equal(reviewed.status, "pass", reviewed.errors.join("\n"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
