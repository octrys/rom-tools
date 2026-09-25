// Detail pane for the selected packet: header chips, hex, interpreter, fields.
import { html, useState, useEffect, useMemo } from "../lib/preact.js";
import { direction, hexToBytes, isUnknownFrame } from "../lib/format.js";
import { CopyButton, Icon, Segmented } from "./common.js";
import { HexView, Interpreter } from "./hex.js";
import { FieldTree } from "./tree.js";

const FIELD_VIEW_OPTIONS = [
    { value: "tree", label: "Tree" },
    { value: "json", label: "JSON" },
];

export function DetailPane({ frame, index, onPrev, onNext, hasPrev, hasNext }) {
    const [selected, setSelected] = useState(null);
    const [little, setLittle] = useState(true);
    const [mode, setMode] = useState("tree"); // tree | json

    useEffect(() => setSelected(null), [frame]);

    const bytes = useMemo(() => hexToBytes(frame.body_hex), [frame.body_hex]);
    const dir = direction(frame.direction);
    const jsonText = useMemo(
        () => JSON.stringify(frame.fields || [], null, 2),
        [frame.fields],
    );

    return html`<section class="detail">
        <div class="detail-head">
            <div class="meta">
                <div class="detail-title">
                    #${index} · ${frame.opcode_hex}${" "}
                    <span class=${isUnknownFrame(frame) ? "name-unknown" : "name-known"}
                        >${frame.name || "unknown"}</span
                    >
                </div>
                <div class="detail-chips">
                    <span class="chip"><span class=${dir.cls}>●</span> ${dir.label}</span>
                    <span class="chip">${frame.body_len} bytes</span>
                    <span class="chip">t = ${frame.t != null ? frame.t.toFixed(3) : "?"}s</span>
                    <span class="chip"
                        >${frame.plaintext ? "plaintext" : "decrypted"}</span
                    >
                    ${frame.error
                        ? html`<span class="chip text-err">${frame.error}</span>`
                        : null}
                </div>
            </div>
            <button class="ibtn" aria-label="Previous packet" disabled=${!hasPrev} onClick=${onPrev}>
                <${Icon} path=${["M15 18l-6-6 6-6"]} />
            </button>
            <button class="ibtn" aria-label="Next packet" disabled=${!hasNext} onClick=${onNext}>
                <${Icon} path=${["M9 18l6-6-6-6"]} />
            </button>
        </div>

        <${HexView}
            bytes=${bytes}
            bodyLen=${frame.body_len}
            selected=${selected}
            onSelect=${(off) => setSelected((prev) => (prev === off ? null : off))}
        />

        <${Interpreter}
            bytes=${bytes}
            selected=${selected}
            little=${little}
            setLittle=${setLittle}
        />

        <div class="treecard">
            <div class="tree-toolbar">
                <${Segmented} options=${FIELD_VIEW_OPTIONS} value=${mode} onChange=${setMode} />
                <span class="grow"></span>
                <span class="schema">${(frame.fields || []).length} fields</span>
                <${CopyButton} text=${jsonText} label="copy JSON" />
            </div>
            ${mode === "tree"
                ? html`<${FieldTree} fields=${frame.fields} />`
                : html`<pre class="json-view">${jsonText}</pre>`}
        </div>
    </section>`;
}
