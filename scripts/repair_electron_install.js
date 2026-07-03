#!/usr/bin/env node
/** Repair incomplete electron npm installs — unzip cached artifact with absolute paths. */
const { downloadArtifact } = require("@electron/get");
const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..", "node_modules", "electron");
const version = require(path.join(root, "package.json")).version;
const platformPath = "Electron.app/Contents/MacOS/Electron";

async function main() {
  const zip = await downloadArtifact({
    version,
    artifactName: "electron",
    platform: process.platform,
    arch: process.arch,
    force: false,
  });
  const dist = path.resolve(root, "dist");
  fs.rmSync(dist, { recursive: true, force: true });
  fs.mkdirSync(dist, { recursive: true });
  execFileSync("unzip", ["-q", "-o", zip, "-d", dist], { stdio: "inherit" });
  fs.writeFileSync(path.join(root, "path.txt"), platformPath);
  fs.writeFileSync(path.join(dist, "version"), version);
  const frameworks = path.join(dist, "Electron.app", "Contents", "Frameworks");
  console.log(
    "electron repair ok:",
    fs.readdirSync(frameworks).slice(0, 3).join(", ")
  );
}

main().catch((err) => {
  console.error("electron repair failed:", err);
  process.exit(1);
});
