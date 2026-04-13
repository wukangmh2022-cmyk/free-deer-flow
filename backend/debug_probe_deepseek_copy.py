from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from app.deepseek_local_provider import MODEL_SPECS
from debug_compare_provider_cases import LARGE_PYTHON_FIXTURE, WRITE_FILE_TOOL
from deerflow.models.deepseek_web_bridge import COPY_CAPTURE_INIT_SCRIPT, DeepSeekWebBridge


COPY_PROBE_INIT_SCRIPT = """
() => {
  if (window.__deerflowCopyProbeInstalled) {
    return;
  }
  window.__deerflowCopyProbeInstalled = true;
  window.__deerflowCopyProbe = { events: [] };

  const push = (type, extra = {}) => {
    window.__deerflowCopyProbe.events.push({
      type,
      ts: Date.now(),
      ...extra,
    });
  };

  document.addEventListener(
    'copy',
    (event) => {
      let text = '';
      try {
        text = window.getSelection ? String(window.getSelection()) : '';
      } catch {
        text = '';
      }
      push('document.copy', {
        selectionPreview: text.slice(0, 200),
      });
    },
    true,
  );

  const originalExecCommand = document.execCommand ? document.execCommand.bind(document) : null;
  if (originalExecCommand && !document.__deerflowExecWrapped) {
    document.execCommand = function(command, ...args) {
      push('document.execCommand', { command });
      return originalExecCommand(command, ...args);
    };
    document.__deerflowExecWrapped = true;
  }

  const clipboard = navigator.clipboard;
  if (clipboard && typeof clipboard.writeText === 'function' && !clipboard.__deerflowProbeWriteTextWrapped) {
    const originalWriteText = clipboard.writeText.bind(clipboard);
    clipboard.writeText = async (text) => {
      push('clipboard.writeText', {
        textPreview: typeof text === 'string' ? text.slice(0, 200) : '',
        textLength: typeof text === 'string' ? text.length : null,
      });
      return await originalWriteText(text);
    };
    clipboard.__deerflowProbeWriteTextWrapped = true;
  }

  if (clipboard && typeof clipboard.write === 'function' && !clipboard.__deerflowProbeWriteWrapped) {
    const originalWrite = clipboard.write.bind(clipboard);
    clipboard.write = async (items) => {
      push('clipboard.write', {
        itemCount: Array.isArray(items) ? items.length : null,
      });
      return await originalWrite(items);
    };
    clipboard.__deerflowProbeWriteWrapped = true;
  }
}
"""


def build_large_case_messages() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return (
        [
            {
                "role": "user",
                "content": (
                    "Use exactly one write_file tool call.\n"
                    "Do not add assistant text.\n"
                    "Set path to /tmp/provider_case_100.py.\n"
                    "Set content exactly to the following code:\n"
                    f"{LARGE_PYTHON_FIXTURE}"
                ),
            }
        ],
        [WRITE_FILE_TOOL],
    )


def wait_for_generation(bridge: DeepSeekWebBridge, page, before_count: int, before_text: str) -> dict[str, Any]:
    locator = bridge.assistant_locator(page)
    deadline = time.time() + 180
    started = False
    stable_seen = 0
    last_text = ""
    last_log = 0.0

    while time.time() < deadline:
        current_count = locator.count()
        current_text = bridge.last_assistant_text(locator)
        if not started:
            if current_count > before_count or (current_text and current_text != before_text):
                started = True
            else:
                page.wait_for_timeout(300)
                continue

        now = time.time()
        can_submit = bridge.can_submit_next_turn(page)
        if now - last_log >= 5:
            print(
                f"[probe] progress assistant_count={current_count} assistant_chars={len(current_text)} stable_seen={stable_seen} can_submit={can_submit}",
                file=sys.stderr,
                flush=True,
            )
            last_log = now

        if current_text and current_text == last_text:
            stable_seen += 1
        else:
            stable_seen = 0
            last_text = current_text

        if can_submit and current_text and stable_seen >= 2:
            page.wait_for_timeout(1000)
            return {
                "assistant_count": current_count,
                "assistant_chars": len(current_text),
                "assistant_preview": current_text[:500],
                "ready_reason": "submit_ready",
            }

        if current_text and stable_seen >= 10:
            page.wait_for_timeout(1000)
            return {
                "assistant_count": current_count,
                "assistant_chars": len(current_text),
                "assistant_preview": current_text[:500],
                "ready_reason": "stable_text_no_submit",
            }

        page.wait_for_timeout(500)

    raise TimeoutError("Timed out waiting for DeepSeek response to finish generating.")


def inspect_assistant_area(page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
            const selectors = [
              '[data-message-author-role="assistant"]',
              '[data-role="assistant"]',
              '.ds-markdown',
              '.markdown',
              '[class*="message"]',
            ];

            const elements = [];
            for (const selector of selectors) {
              for (const node of document.querySelectorAll(selector)) {
                elements.push(node);
              }
            }
            const el = elements[elements.length - 1];
            if (!el) {
              return { assistantFound: false };
            }

            const visibleTextNear = (button) => {
              const rect = button.getBoundingClientRect();
              const centerX = rect.left + rect.width / 2;
              const centerY = rect.top + rect.height / 2;
              const out = [];
              for (const node of document.body.querySelectorAll('*')) {
                if (!(node instanceof HTMLElement) || node === button || node.contains(button)) {
                  continue;
                }
                const style = window.getComputedStyle(node);
                if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') {
                  continue;
                }
                const text = (node.innerText || node.textContent || '').trim();
                if (!text || text.length > 80) {
                  continue;
                }
                const nodeRect = node.getBoundingClientRect();
                if (!nodeRect.width || !nodeRect.height) {
                  continue;
                }
                const nodeCenterX = nodeRect.left + nodeRect.width / 2;
                const nodeCenterY = nodeRect.top + nodeRect.height / 2;
                const distance = Math.abs(nodeCenterX - centerX) + Math.abs(nodeCenterY - centerY);
                if (distance > 220) {
                  continue;
                }
                out.push({
                  text,
                  distance,
                  tag: node.tagName.toLowerCase(),
                  className: typeof node.className === 'string' ? node.className : '',
                });
              }
              out.sort((a, b) => a.distance - b.distance);
              return out.slice(0, 8);
            };

            const chain = [];
            let parent = el;
            for (let depth = 0; depth < 8 && parent; depth += 1) {
              const rect = parent.getBoundingClientRect();
              const children = [...parent.children].slice(0, 12).map((child) => ({
                tag: child.tagName.toLowerCase(),
                className: typeof child.className === 'string' ? child.className : '',
                buttonCount: child.querySelectorAll('button,[role="button"],div[role="button"]').length,
                textPreview: ((child instanceof HTMLElement ? child.innerText : child.textContent) || '').trim().slice(0, 120),
              }));
              chain.push({
                depth,
                tag: parent.tagName.toLowerCase(),
                className: typeof parent.className === 'string' ? parent.className : '',
                role: parent.getAttribute('role') || '',
                buttonCount: parent.querySelectorAll('button,[role="button"],div[role="button"]').length,
                rect: {
                  top: rect.top,
                  left: rect.left,
                  width: rect.width,
                  height: rect.height,
                },
                children,
              });
              parent = parent.parentElement;
            }

            const targetRect = el.getBoundingClientRect();
            const buttonSelector = 'button,[role="button"],div[role="button"]';
            const candidates = [];
            let probeId = 0;
            for (const button of document.querySelectorAll(buttonSelector)) {
              if (!(button instanceof HTMLElement)) {
                continue;
              }
              const style = window.getComputedStyle(button);
              if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') {
                continue;
              }
              const rect = button.getBoundingClientRect();
              if (!rect.width || !rect.height) {
                continue;
              }
              const label = [
                button.getAttribute('aria-label') || '',
                button.getAttribute('title') || '',
                button.innerText || '',
                button.textContent || '',
              ].join(' ').trim();
              const distance = Math.abs(rect.top - targetRect.bottom) + Math.abs(rect.left - targetRect.left);
              button.dataset.deerflowProbeId = String(probeId);
              candidates.push({
                probeId: String(probeId),
                label,
                className: typeof button.className === 'string' ? button.className : '',
                html: button.outerHTML.slice(0, 240),
                rect: {
                  top: rect.top,
                  left: rect.left,
                  width: rect.width,
                  height: rect.height,
                },
                distance,
                parentClassName: button.parentElement && typeof button.parentElement.className === 'string'
                  ? button.parentElement.className
                  : '',
                surroundingTexts: visibleTextNear(button),
              });
              probeId += 1;
            }

            candidates.sort((a, b) => a.distance - b.distance);

            return {
              assistantFound: true,
              assistantRect: {
                top: targetRect.top,
                left: targetRect.left,
                width: targetRect.width,
                height: targetRect.height,
              },
              parentChain: chain,
              candidateButtons: candidates.slice(0, 20),
            };
        }"""
    )


def collect_hover_tooltips(page, probe_id: str) -> list[dict[str, Any]]:
    locator = page.locator(f'[data-deerflow-probe-id="{probe_id}"]').first
    locator.hover(timeout=1500)
    page.wait_for_timeout(350)
    return page.evaluate(
        """(probeId) => {
            const button = document.querySelector(`[data-deerflow-probe-id="${probeId}"]`);
            if (!(button instanceof HTMLElement)) {
              return [];
            }
            const rect = button.getBoundingClientRect();
            const centerX = rect.left + rect.width / 2;
            const centerY = rect.top + rect.height / 2;
            const out = [];
            for (const node of document.body.querySelectorAll('*')) {
              if (!(node instanceof HTMLElement) || node === button || node.contains(button)) {
                continue;
              }
              const style = window.getComputedStyle(node);
              if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') {
                continue;
              }
              const text = (node.innerText || node.textContent || '').trim();
              if (!text || text.length > 80) {
                continue;
              }
              const nodeRect = node.getBoundingClientRect();
              if (!nodeRect.width || !nodeRect.height) {
                continue;
              }
              const nodeCenterX = nodeRect.left + nodeRect.width / 2;
              const nodeCenterY = nodeRect.top + nodeRect.height / 2;
              const distance = Math.abs(nodeCenterX - centerX) + Math.abs(nodeCenterY - centerY);
              if (distance > 220) {
                continue;
              }
              out.push({
                text,
                distance,
                tag: node.tagName.toLowerCase(),
                className: typeof node.className === 'string' ? node.className : '',
              });
            }
            out.sort((a, b) => a.distance - b.distance);
            return out.slice(0, 10);
        }""",
        probe_id,
    )


def click_candidate(page, probe_id: str) -> dict[str, Any]:
    page.evaluate(COPY_PROBE_INIT_SCRIPT)
    page.evaluate(COPY_CAPTURE_INIT_SCRIPT)
    page.evaluate(
        """() => {
            window.__deerflowCopyProbe = { events: [] };
            window.__deerflowCopyEvents = [];
        }"""
    )
    locator = page.locator(f'[data-deerflow-probe-id="{probe_id}"]').first
    locator.click(timeout=1500)
    page.wait_for_timeout(600)
    probe_events = page.evaluate(
        "() => (window.__deerflowCopyProbe && Array.isArray(window.__deerflowCopyProbe.events)) ? window.__deerflowCopyProbe.events : []"
    )
    copied_text = page.evaluate(
        """() => {
            const events = window.__deerflowCopyEvents || [];
            const item = events[events.length - 1];
            return item && typeof item.text === 'string' ? item.text : '';
        }"""
    )
    return {
        "probe_events": probe_events,
        "copied_text_preview": copied_text[:500] if isinstance(copied_text, str) else "",
        "copied_text_length": len(copied_text) if isinstance(copied_text, str) else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe DeepSeek assistant-card copy controls.")
    parser.add_argument("--visible", action="store_true", help="Run Chromium non-headless.")
    parser.add_argument("--inspect-url", help="Inspect an existing DeepSeek chat URL without sending a new prompt.")
    args = parser.parse_args()

    spec = MODEL_SPECS["deepseek-web-deerflow"]
    profile_dir = str(Path(spec.profile_dir).expanduser().resolve())
    bridge = DeepSeekWebBridge(
        user_data_dir=profile_dir,
        headless=not args.visible,
        force_new_chat=True,
        sticky_marker=spec.sticky_marker,
        sticky_reanchor_messages=spec.sticky_reanchor_messages,
    )

    messages, tools = build_large_case_messages()

    try:
        print("[probe] opening page", file=sys.stderr, flush=True)
        page = bridge.ensure_page(visible=args.visible)
        page.add_init_script(COPY_PROBE_INIT_SCRIPT)
        page.evaluate(COPY_PROBE_INIT_SCRIPT)
        if args.inspect_url:
            print(f"[probe] opening existing url={args.inspect_url}", file=sys.stderr, flush=True)
            page.goto(args.inspect_url, wait_until="domcontentloaded", timeout=bridge.page_load_timeout_ms)
            page.wait_for_timeout(2000)
            assistant_locator = bridge.assistant_locator(page)
            current_text = bridge.last_assistant_text(assistant_locator)
            generation = {
                "assistant_count": assistant_locator.count(),
                "assistant_chars": len(current_text),
                "assistant_preview": current_text[:500],
                "ready_reason": "inspect_existing_url",
            }
        else:
            print("[probe] ensuring chat ready", file=sys.stderr, flush=True)
            bridge.ensure_chat_ready(page)
            bridge.best_effort_start_new_chat(page)

            print("[probe] locating input", file=sys.stderr, flush=True)
            input_box = bridge.first_visible(page, bridge.input_selectors)
            assistant_locator = bridge.assistant_locator(page)
            before_count = assistant_locator.count()
            before_text = bridge.last_assistant_text(assistant_locator)

            prompt = bridge.build_full_prompt(messages=messages, tools=tools)
            print("[probe] submitting prompt", file=sys.stderr, flush=True)
            bridge.fill_input(input_box, prompt)
            if not bridge.try_submit(page, input_box):
                raise RuntimeError("Failed to submit prompt.")

            print("[probe] waiting for generation", file=sys.stderr, flush=True)
            generation = wait_for_generation(bridge, page, before_count, before_text)
            print(
                f"[probe] generation done assistant_chars={generation['assistant_chars']}",
                file=sys.stderr,
                flush=True,
            )
        print("[probe] inspecting assistant area", file=sys.stderr, flush=True)
        inspection = inspect_assistant_area(page)
        print(
            f"[probe] inspection candidate_buttons={len(inspection.get('candidateButtons', []))}",
            file=sys.stderr,
            flush=True,
        )
        page.evaluate(COPY_PROBE_INIT_SCRIPT)
        probe_events = page.evaluate(
            "() => (window.__deerflowCopyProbe && Array.isArray(window.__deerflowCopyProbe.events)) ? window.__deerflowCopyProbe.events : []"
        )

        hovered_candidates: list[dict[str, Any]] = []
        for candidate in inspection.get("candidateButtons", [])[:8]:
            probe_id = candidate.get("probeId")
            if not isinstance(probe_id, str):
                continue
            print(
                f"[probe] hovering candidate probe_id={probe_id} distance={candidate.get('distance')}",
                file=sys.stderr,
                flush=True,
            )
            hover_texts = collect_hover_tooltips(page, probe_id)
            hovered_candidates.append(
                {
                    "probeId": probe_id,
                    "distance": candidate.get("distance"),
                    "className": candidate.get("className"),
                    "label": candidate.get("label"),
                    "hoverTexts": hover_texts,
                    "html": candidate.get("html"),
                }
            )

        copy_click_result: dict[str, Any] | None = None
        for candidate in hovered_candidates:
            hover_texts = candidate.get("hoverTexts", [])
            if any((item.get("text") or "").strip().lower() in {"copy", "复制"} for item in hover_texts):
                probe_id = candidate.get("probeId")
                if isinstance(probe_id, str):
                    print(f"[probe] clicking copy candidate probe_id={probe_id}", file=sys.stderr, flush=True)
                    copy_click_result = click_candidate(page, probe_id)
                break

        result = {
            "page_url": page.url,
            "profile_dir": profile_dir,
            "generation": generation,
            "inspection": inspection,
            "hovered_candidates": hovered_candidates,
            "copy_probe_events": probe_events,
            "copy_click_result": copy_click_result,
        }
        print("[probe] writing result", file=sys.stderr, flush=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        bridge.close()


if __name__ == "__main__":
    raise SystemExit(main())
