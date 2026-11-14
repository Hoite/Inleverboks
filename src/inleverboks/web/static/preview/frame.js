// Preview renderer inside a sandboxed iframe (opaque origin, own CSP). It receives the file
// as an ArrayBuffer from the parent page and never sends anything anywhere.
(function () {
  "use strict";
  var out = document.getElementById("out");
  var MAX_TEXT = 1024 * 1024;
  var MAX_ROWS = 500;
  var TEXT_EXT = ["txt", "md", "csv", "py", "sh", "js", "ts", "html", "css", "java", "c", "h", "cpp", "cs",
    "sql", "json", "yaml", "yml", "xml", "ini", "cfg", "conf", "log", "php", "rb", "go", "rs", "kt", "swift", "bat", "ps1"];
  var IMAGE_EXT = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp" };
  var currentUrl = null;

  function clear() {
    if (currentUrl) { URL.revokeObjectURL(currentUrl); currentUrl = null; }
    out.textContent = "";
  }

  function message(text) {
    var p = document.createElement("p");
    p.className = "message";
    p.textContent = text;
    out.appendChild(p);
  }

  function decodeText(buffer) {
    try { return new TextDecoder("utf-8", { fatal: true }).decode(buffer); }
    catch (e) { return new TextDecoder("windows-1252").decode(buffer); }
  }

  function renderText(buffer) {
    var pre = document.createElement("pre");
    var slice = buffer.byteLength > MAX_TEXT ? buffer.slice(0, MAX_TEXT) : buffer;
    pre.textContent = decodeText(slice) + (buffer.byteLength > MAX_TEXT ? "\n…" : "");
    out.appendChild(pre);
  }

  function renderImage(buffer, type) {
    currentUrl = URL.createObjectURL(new Blob([buffer], { type: type }));
    var img = document.createElement("img");
    img.alt = "";
    img.src = currentUrl;
    out.appendChild(img);
  }

  function renderDocx(buffer, texts) {
    var container = document.createElement("div");
    out.appendChild(container);
    return window.docx.renderAsync(buffer, container, null, {
      inWrapper: true, ignoreLastRenderedPageBreak: true, useBase64URL: true, experimental: false,
    }).catch(function () { clear(); message(texts.noPreview); });
  }

  function renderXlsx(buffer) {
    var workbook = window.XLSX.read(buffer, { type: "array", cellFormula: false, cellHTML: false });
    workbook.SheetNames.forEach(function (name) {
      var sheet = workbook.Sheets[name];
      var rows = window.XLSX.utils.sheet_to_json(sheet, { header: 1, raw: false, blankrows: false });
      var section = document.createElement("section");
      section.className = "sheet";
      var title = document.createElement("h2");
      title.textContent = name;
      section.appendChild(title);
      var table = document.createElement("table");
      rows.slice(0, MAX_ROWS).forEach(function (row) {
        var tr = document.createElement("tr");
        for (var i = 0; i < row.length; i++) {
          var td = document.createElement("td");
          td.textContent = row[i] == null ? "" : String(row[i]);
          tr.appendChild(td);
        }
        table.appendChild(tr);
      });
      section.appendChild(table);
      out.appendChild(section);
    });
  }

  function renderZip(buffer, texts) {
    return window.JSZip.loadAsync(buffer).then(function (zip) {
      var wrap = document.createElement("div");
      wrap.className = "zip";
      var note = document.createElement("p");
      note.className = "note";
      note.textContent = texts.zipNote;
      wrap.appendChild(note);
      var list = document.createElement("ul");
      Object.keys(zip.files).sort().forEach(function (path) {
        var li = document.createElement("li");
        li.textContent = path;
        list.appendChild(li);
      });
      wrap.appendChild(list);
      out.appendChild(wrap);
    });
  }

  var pdfjsPromise = null;
  function loadPdfjs() {
    if (!pdfjsPromise) {
      var base = new URL("../vendor/pdfjs/", document.baseURI).href;
      // The frame has an opaque origin, where module workers cannot start. pdf.js then runs
      // its worker code in this (isolated) frame instead: it uses globalThis.pdfjsWorker
      // when that is set before the first document is opened.
      pdfjsPromise = import(base + "pdf.worker.min.mjs").then(function (worker) {
        window.pdfjsWorker = worker;
        return import(base + "pdf.min.mjs");
      });
    }
    return pdfjsPromise;
  }

  function renderPdf(buffer, texts) {
    return loadPdfjs().then(function (pdfjs) {
      return pdfjs.getDocument({ data: new Uint8Array(buffer), isEvalSupported: false, enableXfa: false }).promise;
    }).then(function (pdf) {
      var chain = Promise.resolve();
      var pages = Math.min(pdf.numPages, 50);
      for (var n = 1; n <= pages; n++) {
        (function (number) {
          chain = chain.then(function () { return pdf.getPage(number); }).then(function (page) {
            var viewport = page.getViewport({ scale: 1.3 });
            var canvas = document.createElement("canvas");
            canvas.width = viewport.width;
            canvas.height = viewport.height;
            out.appendChild(canvas);
            return page.render({ canvasContext: canvas.getContext("2d"), viewport: viewport, canvas: canvas }).promise;
          });
        })(n);
      }
      return chain;
    }).catch(function (error) {
      console.warn("pdf preview failed", error && error.message ? error.message : error);
      clear();
      message(texts.noPreview);
    });
  }

  window.addEventListener("message", function (event) {
    if (event.source !== window.parent || !event.data || event.data.type !== "render") return;
    var data = event.data;
    var texts = data.text || { noPreview: "", zipNote: "" };
    clear();
    try {
      var ext = data.ext;
      if (ext === "pdf") return renderPdf(data.buffer, texts);
      if (ext === "docx") return renderDocx(data.buffer, texts);
      if (ext === "xlsx" || ext === "xls" || ext === "ods") return renderXlsx(data.buffer);
      if (ext === "zip") return renderZip(data.buffer, texts).catch(function () { clear(); message(texts.noPreview); });
      if (IMAGE_EXT[ext]) return renderImage(data.buffer, IMAGE_EXT[ext]);
      if (TEXT_EXT.indexOf(ext) !== -1) return renderText(data.buffer);
      message(texts.noPreview);
    } catch (e) {
      clear();
      message(texts.noPreview);
    }
  });

  window.parent.postMessage({ type: "ready" }, "*");
})();
