// Public API surface of the Node SDK at a source directory, as a flat map
// `"Name" | "Name.member" -> normalized type text`. Used by the release script to decide
// whether a release changes the public API. Uses the SDK's own TypeScript install.
import { createRequire } from "node:module";
import path from "node:path";

const IMPORT_PREFIX = /import\("[^"]*"\)\./g;

function loadTypeScript(sdkDir) {
  return createRequire(path.join(sdkDir, "package.json"))("typescript");
}

/**
 * @param {string} srcDir directory containing `index.ts` (a snapshot of `sdks/node/src`)
 * @param {string} sdkDir `sdks/node` of the working tree (for the TypeScript install)
 * @returns {Record<string, string>}
 */
export function nodeApiSurface(srcDir, sdkDir) {
  const ts = loadTypeScript(sdkDir);
  const entry = path.join(srcDir, "index.ts");
  const program = ts.createProgram([entry], {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.NodeNext,
    moduleResolution: ts.ModuleResolutionKind.NodeNext,
    lib: ["lib.es2023.d.ts", "lib.dom.d.ts"],
    strict: true,
    noEmit: true,
    skipLibCheck: true,
    types: [],
  });
  const checker = program.getTypeChecker();
  const source = program.getSourceFile(entry);
  if (!source) throw new Error(`no index.ts in ${srcDir}`);
  const moduleSymbol = checker.getSymbolAtLocation(source);
  if (!moduleSymbol) throw new Error(`index.ts in ${srcDir} has no exports`);

  const root = path.resolve(srcDir);
  const own = (sym) => (sym?.declarations ?? []).some((d) => path.resolve(d.getSourceFile().fileName).startsWith(root));
  const flags = ts.TypeFormatFlags.NoTruncation | ts.TypeFormatFlags.WriteArrowStyleSignature;
  const fmt = (type, at) => checker.typeToString(type, at, flags).replace(IMPORT_PREFIX, "");
  const out = {};
  const seen = new Set();
  const queue = [];

  const enqueue = (type, depth = 0) => {
    if (!type || depth > 4) return;
    const sym = type.aliasSymbol ?? type.getSymbol();
    if (sym && own(sym) && !seen.has(sym)) {
      seen.add(sym);
      queue.push(sym);
    }
    if (type.isUnionOrIntersection()) for (const t of type.types) enqueue(t, depth + 1);
    for (const t of type.aliasTypeArguments ?? []) enqueue(t, depth + 1);
    for (const t of checker.getTypeArguments?.(type) ?? []) enqueue(t, depth + 1);
    for (const sig of [...type.getCallSignatures(), ...type.getConstructSignatures()]) {
      for (const p of sig.getParameters()) enqueue(checker.getTypeOfSymbol(p), depth + 1);
      enqueue(sig.getReturnType(), depth + 1);
    }
  };

  const isPublicMember = (member) => {
    if (member.name.startsWith("#") || member.name.startsWith("_") || member.name.startsWith("__")) return false;
    const mods = ts.getCombinedModifierFlags?.(member.valueDeclaration ?? member.declarations?.[0] ?? source) ?? 0;
    return !(mods & (ts.ModifierFlags.Private | ts.ModifierFlags.Protected));
  };

  const members = (name, type, at) => {
    for (const member of checker.getPropertiesOfType(type)) {
      if (!isPublicMember(member)) continue;
      const decl = member.valueDeclaration ?? member.declarations?.[0] ?? at;
      const memberType = checker.getTypeOfSymbolAtLocation(member, decl);
      const optional = member.flags & ts.SymbolFlags.Optional ? "?" : "";
      out[`${name}.${member.name}${optional}`] = fmt(memberType, decl);
      enqueue(memberType);
    }
  };

  const describe = (name, sym) => {
    const decl = sym.declarations?.[0] ?? source;
    if (sym.flags & ts.SymbolFlags.TypeAlias) {
      const type = checker.getDeclaredTypeOfSymbol(sym);
      out[name] = `type ${checker.typeToString(type, decl, flags | ts.TypeFormatFlags.InTypeAlias).replace(IMPORT_PREFIX, "")}`;
      enqueue(type);
      if (type.isUnionOrIntersection() || !(type.flags & ts.TypeFlags.Object)) return;
      members(name, type, decl);
      return;
    }
    if (sym.flags & ts.SymbolFlags.Interface && !(sym.flags & ts.SymbolFlags.Class)) {
      out[name] = "interface";
      members(name, checker.getDeclaredTypeOfSymbol(sym), decl);
      return;
    }
    if (sym.flags & ts.SymbolFlags.Class) {
      const staticType = checker.getTypeOfSymbolAtLocation(sym, decl);
      out[name] = `class ${staticType.getConstructSignatures().map((s) => checker.signatureToString(s, decl, flags)).join(" | ")}`.replace(IMPORT_PREFIX, "");
      for (const sig of staticType.getConstructSignatures()) for (const p of sig.getParameters()) enqueue(checker.getTypeOfSymbol(p));
      members(`${name}.static`, staticType, decl);
      members(name, checker.getDeclaredTypeOfSymbol(sym), decl);
      return;
    }
    if (sym.flags & ts.SymbolFlags.Enum) {
      out[name] = `enum ${(sym.exports ? [...sym.exports.keys()] : []).sort().join(",")}`;
      return;
    }
    if (sym.flags & (ts.SymbolFlags.Function | ts.SymbolFlags.Variable)) {
      const type = checker.getTypeOfSymbolAtLocation(sym, decl);
      out[name] = fmt(type, decl);
      enqueue(type);
    }
  };

  for (const exported of checker.getExportsOfModule(moduleSymbol)) {
    const sym = exported.flags & ts.SymbolFlags.Alias ? checker.getAliasedSymbol(exported) : exported;
    seen.add(sym);
    describe(exported.name, sym);
  }
  // Types reachable from the exports (e.g. `GabrielClient.chat: ChatApi`) are public too.
  while (queue.length) {
    const sym = queue.shift();
    if (!(sym.name in out) && sym.name !== "__type" && sym.name !== "__object") describe(sym.name, sym);
  }
  return Object.fromEntries(Object.entries(out).sort(([a], [b]) => a.localeCompare(b)));
}
