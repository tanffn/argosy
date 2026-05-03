#!/usr/bin/env python3
"""
drawio_export.py — Export draw.io files to PNG using headless Chromium.

Renders each .drawio file via the draw.io viewer app (app.diagrams.net),
then takes a Playwright screenshot of the rendered diagram — this means
html=1 cells, whiteSpace=wrap, swimlane headers, etc. all render exactly
as they appear in draw.io desktop.

Default output: PNGs are written next to the .drawio source files in the
same directory. This matches what authors expect when embedding in
markdown via `![caption](diagrams/<name>.png)` (pandoc resolves relative
to the markdown's directory; the report's "diagrams/" subfolder holds
both .drawio sources and .png exports together).

Pass `--out-subdir <name>` to keep the older behavior of writing PNGs to
a subdirectory (e.g. `--out-subdir PNG_Output`).

Usage:
    python3 drawio_export.py <diagrams_dir> [--scale 2] [--file name.drawio] [--out-subdir SUBDIR]

Requirements:
    pip install playwright
    playwright install chromium
"""

import argparse
import base64
import os
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("ERROR: playwright not installed.")
    print("Run: python3 -m pip install playwright && python3 -m playwright install chromium")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Minimal self-contained draw.io viewer HTML
# Uses the draw.io viewer.min.js to render the XML, then screenshots the result.
# ---------------------------------------------------------------------------
VIEWER_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #ffffff; }}
  #app {{ position: relative; overflow: hidden; }}
  .geEditor {{ display: none !important; }}
</style>
</head>
<body>
<div id="app"></div>
<script src="https://cdn.jsdelivr.net/npm/mxgraph@4.2.2/javascript/mxClient.min.js"></script>
<script>
(function() {{
  var xmlData = new TextDecoder('utf-8').decode(Uint8Array.from(atob("{b64xml}"), c => c.charCodeAt(0)));
  var scale   = {scale};

  // Parse the mxfile wrapper
  var parser   = new DOMParser();
  var xmlDoc   = parser.parseFromString(xmlData, "text/xml");

  // Support <mxfile><diagram>...</diagram></mxfile> and bare <mxGraphModel>
  var modelXml;
  var diagEl = xmlDoc.querySelector("diagram");
  if (diagEl) {{
    var inner = diagEl.textContent.trim();
    try {{
      var innerDoc = parser.parseFromString(inner, "text/xml");
      modelXml = innerDoc.documentElement;
    }} catch(e) {{
      modelXml = xmlDoc.querySelector("mxGraphModel");
    }}
    if (!modelXml || modelXml.nodeName !== "mxGraphModel") {{
      modelXml = xmlDoc.querySelector("mxGraphModel");
    }}
  }} else {{
    modelXml = xmlDoc.querySelector("mxGraphModel");
  }}

  if (!modelXml) {{
    window._exportError = "No mxGraphModel found";
    return;
  }}

  // Create container sized to page
  var pageW = parseInt(modelXml.getAttribute("pageWidth")  || "1400");
  var pageH = parseInt(modelXml.getAttribute("pageHeight") || "900");
  var container = document.getElementById("app");
  container.style.width  = pageW + "px";
  container.style.height = pageH + "px";

  // Disable touch / pointer UI
  mxClient.IS_POINTER = false;

  // Fix swimlane title position: vanilla mxGraph 4.x getLabelBounds returns the
  // BODY area (below the header) for the label, but draw.io renders titles in the
  // HEADER strip.  Override to return just the startSize header area so that the
  // SVG title text lands in the coloured header bar, not floating in the content.
  mxSwimlane.prototype.getLabelBounds = function(rect) {{
    var start = this.getStartSize(rect.width, rect.height);
    var b = new mxRectangle(rect.x, rect.y, rect.width, rect.height);
    if (this.isHorizontal()) {{ b.height = start.height; }}
    else                      {{ b.width  = start.width;  }}
    return b;
  }};

  var graph = new mxGraph(container);
  graph.setEnabled(false);
  graph.setCellsEditable(false);
  graph.setConnectable(false);
  graph.setTooltips(false);
  graph.setPanning(false);

  // Enable HTML labels only for cells that explicitly declare html=1 in their style.
  // Swimlane containers do NOT have html=1, so their title renders in SVG — correctly
  // clipped to the startSize header area.  Content cells with html=1 still get
  // proper HTML wrapping.
  graph.isHtmlLabel = function(cell) {{
    var style = this.getCellStyle(cell);
    return mxUtils.getValue(style, 'html', 0) == 1;
  }};

  // Decode and load model
  var codec = new mxCodec(modelXml.ownerDocument || xmlDoc);
  codec.decode(modelXml, graph.getModel());

  // Step 1: reset to scale=1, translate=0 to get model-space bounds.
  // (Calling graph.fit() first would scale the content to the page container,
  //  making getGraphBounds() return fitted-view coords instead of model coords.
  //  That causes double-scaling when we then apply our own scale factor.)
  graph.view.scaleAndTranslate(1, 0, 0);
  var bounds = graph.getGraphBounds();   // bounds are now in model coordinates

  // Step 2: calculate viewport size at the desired output scale.
  var padding = 20;
  var w = Math.ceil(bounds.width  * scale + padding * 2);
  var h = Math.ceil(bounds.height * scale + padding * 2);

  container.style.width  = w + "px";
  container.style.height = h + "px";

  // scaleAndTranslate: viewX = (modelX + tx) * scale
  // We want modelX=bounds.x to appear at viewX=padding → tx = padding/scale - bounds.x
  var tx = (padding / scale) - bounds.x;
  var ty = (padding / scale) - bounds.y;
  graph.view.scaleAndTranslate(scale, tx, ty);

  // Fix swimlane title position.
  // mxGraph positions the label at the CENTER of the full cell (state.text.bounds.y =
  // state.y + state.height/2).  draw.io renders the title in the startSize header strip,
  // so we must shift bounds.y to the center of that strip and shrink bounds.height to
  // startSize*scale, then force a redraw so the title lands in the header bar.
  var allCells = graph.getModel().cells;
  for (var __cid in allCells) {{
    var __cell = allCells[__cid];
    if (!__cell || !graph.getModel().isVertex(__cell)) continue;
    var __st = graph.view.getState(__cell);
    if (!__st || !__st.text || !__st.text.bounds) continue;
    if ((__cell.style || '').indexOf('swimlane') < 0) continue;
    // Parse startSize from the cell style string (default 30)
    var __ss = 30;
    var __m = (__cell.style || '').match(/startSize=([0-9.]+)/);
    if (__m) __ss = parseFloat(__m[1]);
    var __ssPx = __ss * scale;   // startSize in view pixels
    __st.text.bounds.y = __st.y + __ssPx / 2;
    __st.text.bounds.height = __ssPx;
    __st.text.redraw();
  }}

  window._exportWidth  = w;
  window._exportHeight = h;
  // Use rAF so redrawn labels settle before we screenshot
  requestAnimationFrame(function() {{ window._exportReady = true; }});
}})();
</script>
</body>
</html>
"""


def export_drawio_to_png(drawio_path: Path, out_path: Path, scale: float, page) -> bool:
    """Render one .drawio file to PNG using Playwright screenshot. Returns True on success."""
    xml_bytes  = drawio_path.read_bytes()
    b64xml     = base64.b64encode(xml_bytes).decode('ascii')

    html = VIEWER_HTML.format(b64xml=b64xml, scale=scale)

    page.set_content(html, wait_until="domcontentloaded")

    # Wait for the graph to finish rendering
    try:
        page.wait_for_function("() => window._exportReady === true || window._exportError !== undefined",
                               timeout=20000)
    except Exception as e:
        print(f"  TIMEOUT: {drawio_path.name}: {e}")
        return False

    err = page.evaluate("() => window._exportError || null")
    if err:
        print(f"  ERROR: {drawio_path.name}: {err}")
        return False

    w = page.evaluate("() => window._exportWidth")
    h = page.evaluate("() => window._exportHeight")

    # Resize viewport to match diagram exactly, then screenshot
    page.set_viewport_size({"width": max(w, 1), "height": max(h, 1)})

    # Give the browser time to repaint HTML labels at the new size
    page.wait_for_timeout(400)

    png_bytes = page.screenshot(
        clip={"x": 0, "y": 0, "width": w, "height": h},
        full_page=False,
    )

    out_path.write_bytes(png_bytes)
    print(f"  Saved ({w}x{h}px): {out_path.name}")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Export draw.io files to PNG via headless Chromium screenshot")
    parser.add_argument("diagrams_dir", help="Directory containing *.drawio files")
    parser.add_argument("--scale", type=float, default=2.0,
                        help="Zoom scale factor (default 2.0)")
    parser.add_argument("--file", help="Export only this specific .drawio file")
    parser.add_argument("--out-subdir", default="",
                        help="Optional subdirectory inside diagrams_dir to write PNGs to "
                             "(default: write next to .drawio sources). Use --out-subdir PNG_Output "
                             "to keep the legacy behavior.")
    args = parser.parse_args()

    diagrams_dir = Path(args.diagrams_dir).resolve()
    out_dir = diagrams_dir / args.out_subdir if args.out_subdir else diagrams_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.file:
        drawio_files = [diagrams_dir / args.file]
    else:
        drawio_files = sorted(diagrams_dir.glob("*.drawio"))

    if not drawio_files:
        print(f"No .drawio files found in {diagrams_dir}")
        sys.exit(1)

    print(f"Exporting {len(drawio_files)} diagram(s) from {diagrams_dir}")
    print(f"Output: {out_dir}  (scale={args.scale}x)\n")

    ok = fail = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 2000, "height": 2000})
        page    = context.new_page()

        for i, drawio_path in enumerate(drawio_files, 1):
            print(f"[{i}/{len(drawio_files)}] {drawio_path.name}")
            out_path = out_dir / f"{drawio_path.stem}.png"
            if export_drawio_to_png(drawio_path, out_path, args.scale, page):
                ok += 1
            else:
                fail += 1

        context.close()
        browser.close()

    print(f"\nDone: {ok} exported, {fail} failed.")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
