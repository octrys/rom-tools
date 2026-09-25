// Collapsible tree of a frame's decoded fields.
import { html, Fragment, useState, useMemo } from "../lib/preact.js";
import { isContainer } from "../lib/format.js";

function Scalar({ value, unresolved }) {
    if (unresolved) return html`<span class="v-unresolved">unresolved</span>`;
    if (value === null) return html`<span class="v-null">null</span>`;
    const kind = typeof value;
    if (kind === "number") return html`<span class="v-num">${value}</span>`;
    if (kind === "boolean")
        return html`<span class="v-bool">${String(value)}</span>`;
    return html`<span class="v-str"
        >${JSON.stringify(value).slice(1, -1)}</span
    >`;
}

function TreeNode({ name, type, value, unresolved, depth, index }) {
    const container = !unresolved && isContainer(value);
    const [open, setOpen] = useState(depth < 1);
    const indent = `i${Math.min(depth, 3)}`;

    const entries = useMemo(() => {
        if (!container) return [];
        if (Array.isArray(value))
            return value.map((item, i) => ({ key: i, value: item, isIndex: true }));
        return Object.entries(value).map(([key, item]) => ({
            key,
            value: item,
            isIndex: false,
        }));
    }, [value, container]);

    const label =
        index != null
            ? html`<span class="fidx">[${index}]</span>`
            : html`<span class="fname">${name}</span>`;

    if (!container) {
        return html`<div class="tn">
            <span class="tk ${indent}">
                <span class="car"></span>
                ${label}
                <${Scalar} value=${value} unresolved=${unresolved} />
            </span>
            <span class="ftype">${type || ""}</span>
        </div>`;
    }

    const count = Array.isArray(value)
        ? `[ ${value.length} ]`
        : `{ ${entries.length} }`;

    return html`<${Fragment}>
        <div class="tn clickable">
            <span class="tk ${indent}" onClick=${() => setOpen((o) => !o)}>
                <span class="car">${open ? "▾" : "▸"}</span>
                ${label}
                <span class="v-brk">${count}</span>
            </span>
            <span class="ftype">${type || (Array.isArray(value) ? "array" : "object")}</span>
        </div>
        ${open &&
        entries.map(
            (entry) => html`<${TreeNode}
                key=${entry.key}
                name=${entry.isIndex ? null : entry.key}
                index=${entry.isIndex ? entry.key : null}
                value=${entry.value}
                depth=${depth + 1}
            />`,
        )}
    </${Fragment}>`;
}

export function FieldTree({ fields }) {
    if (!fields || !fields.length)
        return html`<div class="list-empty">no decoded fields</div>`;
    return html`<div>
        <div class="tree-colhdr"><span>Field</span><span class="rt">Type</span></div>
        ${fields.map(
            (field, i) => html`<${TreeNode}
                key=${i}
                name=${field.name}
                type=${field.type}
                value=${field.value}
                unresolved=${field.unresolved}
                depth=${0}
            />`,
        )}
    </div>`;
}
