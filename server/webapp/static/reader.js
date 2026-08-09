(() => {
  "use strict";

  const config = window.MangaBridgeReaderConfig || {};
  const app = document.getElementById("reader-app");
  if (!app) return;

  const storage = {
    get(key, fallback = null) {
      try { return localStorage.getItem(key) ?? fallback; } catch (_) { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem(key, String(value)); } catch (_) {}
    },
  };

  const downloadForm = document.getElementById("reader-download-form");
  if (downloadForm) {
    downloadForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = downloadForm.querySelector("button[type='submit']");
      const progress = document.getElementById("download-progress");
      const message = document.getElementById("progress-message");
      const bar = document.getElementById("progress-bar");
      button.disabled = true;
      progress.hidden = false;
      try {
        const response = await fetch(downloadForm.action, {
          method: "POST",
          body: new FormData(downloadForm),
          headers: { Accept: "application/json", "X-Requested-With": "fetch" },
        });
        if (!response.ok) throw new Error(`Could not start download (${response.status})`);
        const job = await response.json();
        const poll = async () => {
          try {
            const statusResponse = await fetch(job.status_url, { cache: "no-store" });
            if (!statusResponse.ok) throw new Error(`Could not read download status (${statusResponse.status})`);
            const status = await statusResponse.json();
            message.textContent = status.error || status.message || "Downloading…";
            if (status.progress_total > 0) {
              bar.style.width = `${Math.min(100, status.progress_current / status.progress_total * 100)}%`;
            }
            if (status.status === "done") {
              bar.style.width = "100%";
              message.textContent = "Ready. Opening…";
              window.location.replace(status.reader_url || job.reader_url || window.location.href);
              return;
            }
            if (status.status === "error") {
              button.disabled = false;
              return;
            }
            window.setTimeout(poll, 1000);
          } catch (error) {
            message.textContent = error.message;
            button.disabled = false;
          }
        };
        poll();
      } catch (error) {
        message.textContent = error.message;
        button.disabled = false;
      }
    });
  }

  const chapterSelect = document.getElementById("chapter-select");
  if (chapterSelect) {
    chapterSelect.addEventListener("change", () => {
      if (chapterSelect.value) window.location.assign(chapterSelect.value);
    });
  }

  const immersiveButtons = Array.from(document.querySelectorAll("[data-immersive-toggle]"));
  let resizeTimer = 0;
  let applyZoomAfterLayout = () => {};
  const setImmersive = async (enabled) => {
    app.classList.toggle("is-immersive", enabled);
    immersiveButtons.forEach((button) => {
      button.textContent = enabled ? "Exit fullscreen" : "Fullscreen";
      button.setAttribute("aria-pressed", enabled ? "true" : "false");
    });
    if (enabled) {
      try {
        if (app.requestFullscreen && !document.fullscreenElement) await app.requestFullscreen({ navigationUI: "hide" });
      } catch (_) {
        // CSS immersive mode remains active if the browser refuses the Fullscreen API.
      }
    } else if (document.fullscreenElement) {
      try { await document.exitFullscreen(); } catch (_) {}
    }
    window.requestAnimationFrame(() => applyZoomAfterLayout(true));
  };
  immersiveButtons.forEach((button) => button.addEventListener("click", () => setImmersive(!app.classList.contains("is-immersive"))));
  document.addEventListener("fullscreenchange", () => {
    if (!document.fullscreenElement && app.classList.contains("is-immersive")) {
      app.classList.remove("is-immersive");
      immersiveButtons.forEach((button) => {
        button.textContent = "Fullscreen";
        button.setAttribute("aria-pressed", "false");
      });
    }
    window.requestAnimationFrame(() => applyZoomAfterLayout(true));
  });

  const pageUrls = Array.isArray(config.pageUrls) ? config.pageUrls : [];
  if (!pageUrls.length) return;

  const scrollView = document.getElementById("scroll-view");
  const readerStrip = document.getElementById("reader-strip");
  const pageView = document.getElementById("page-view");
  const pageFrame = document.getElementById("page-frame");
  const singleImage = document.getElementById("single-page-image");
  const imageStatus = document.getElementById("image-status");
  const modeButton = document.getElementById("mode-toggle");
  const zoomOut = document.getElementById("zoom-out");
  const zoomIn = document.getElementById("zoom-in");
  const zoomReset = document.getElementById("zoom-reset");
  const zoomLabel = document.getElementById("zoom-label");
  const previousPage = document.getElementById("page-prev");
  const nextPage = document.getElementById("page-next");
  const pageCounter = document.getElementById("page-counter");
  const firstPage = document.getElementById("first-page");
  const markRead = document.getElementById("mark-read");
  const stripPages = Array.from(document.querySelectorAll(".reader-strip-page"));

  let mode = storage.get("mangabridge-reader-mode-v3", "scroll") === "single" ? "single" : "scroll";
  let pageIndex = Math.max(0, Math.min(pageUrls.length - 1, Number(config.resumePage || 0)));
  let zoom = Math.max(0.5, Math.min(3, Number(storage.get("mangabridge-reader-zoom-v3", "1")) || 1));
  let readState = Boolean(config.currentRead);
  let progressTimer = 0;
  let imageRequest = 0;
  let touchStartX = null;
  let touchStartY = null;

  const saveProgress = async (action, page = pageIndex) => {
    if (!config.currentDownloaded || !config.progressUrl) return;
    try {
      await fetch(config.progressUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": config.csrfToken || "",
          "X-Requested-With": "fetch",
        },
        body: JSON.stringify({ chapter_num: config.chapterNum, action, last_page: page }),
      });
    } catch (_) {
      // Reading remains available if progress persistence is temporarily unavailable.
    }
  };

  const scheduleProgress = (page = pageIndex) => {
    window.clearTimeout(progressTimer);
    progressTimer = window.setTimeout(() => saveProgress("open", page), 300);
  };

  const updateReadButton = () => {
    if (!markRead) return;
    markRead.textContent = readState ? "Read" : "Mark read";
    markRead.classList.toggle("is-active", readState);
  };

  const markLastPageRead = () => {
    if (pageIndex !== pageUrls.length - 1 || readState) return;
    readState = true;
    updateReadButton();
    saveProgress("complete", pageIndex);
  };

  const updateControls = () => {
    if (pageCounter) pageCounter.textContent = `${pageIndex + 1} / ${pageUrls.length}`;
    if (previousPage) previousPage.disabled = pageIndex <= 0;
    if (nextPage) nextPage.disabled = pageIndex >= pageUrls.length - 1;
    if (modeButton) {
      modeButton.textContent = mode === "single" ? "Scroll" : "Single page";
      modeButton.setAttribute("aria-pressed", mode === "single" ? "true" : "false");
    }
    if (zoomLabel) zoomLabel.textContent = `${Math.round(zoom * 100)}%`;
    if (zoomOut) zoomOut.disabled = zoom <= 0.5001;
    if (zoomIn) zoomIn.disabled = zoom >= 2.9999;
    app.dataset.viewMode = mode;
  };

  const preloadAround = (index) => {
    [index - 1, index + 1].forEach((candidate) => {
      if (candidate < 0 || candidate >= pageUrls.length) return;
      const image = new Image();
      image.src = pageUrls[candidate];
    });
  };

  const visibleStripPage = () => {
    if (!stripPages.length) return 0;
    const rootRect = scrollView.getBoundingClientRect();
    const center = rootRect.top + rootRect.height * 0.42;
    let best = { index: pageIndex, distance: Infinity };
    stripPages.forEach((page, index) => {
      const rect = page.getBoundingClientRect();
      const clippedTop = Math.max(rect.top, rootRect.top);
      const clippedBottom = Math.min(rect.bottom, rootRect.bottom);
      if (clippedBottom <= clippedTop) return;
      const pageCenter = clippedTop + (clippedBottom - clippedTop) * 0.35;
      const distance = Math.abs(pageCenter - center);
      if (distance < best.distance) best = { index, distance };
    });
    return best.index;
  };

  const scrollToPage = (index, behavior = "auto") => {
    const target = stripPages[Math.max(0, Math.min(stripPages.length - 1, index))];
    if (!target) return;
    scrollView.scrollTo({ top: target.offsetTop, left: Math.max(0, (scrollView.scrollWidth - scrollView.clientWidth) / 2), behavior });
  };

  const fitWidth = () => {
    const viewport = mode === "single" ? pageFrame : scrollView;
    if (!viewport) return 800;
    return Math.max(240, Math.min(1100, viewport.clientWidth - 24));
  };

  const captureZoomAnchor = () => {
    if (mode === "single") {
      return {
        type: "single",
        x: (pageFrame.scrollLeft + pageFrame.clientWidth / 2) / Math.max(1, pageFrame.scrollWidth),
        y: (pageFrame.scrollTop + pageFrame.clientHeight / 2) / Math.max(1, pageFrame.scrollHeight),
      };
    }
    const index = visibleStripPage();
    const page = stripPages[index];
    return {
      type: "scroll",
      index,
      y: page ? Math.max(0, (scrollView.scrollTop - page.offsetTop) / Math.max(1, page.offsetHeight)) : 0,
      x: (scrollView.scrollLeft + scrollView.clientWidth / 2) / Math.max(1, scrollView.scrollWidth),
    };
  };

  const restoreZoomAnchor = (anchor) => {
    if (!anchor) return;
    if (anchor.type === "single") {
      pageFrame.scrollLeft = Math.max(0, anchor.x * pageFrame.scrollWidth - pageFrame.clientWidth / 2);
      pageFrame.scrollTop = Math.max(0, anchor.y * pageFrame.scrollHeight - pageFrame.clientHeight / 2);
      return;
    }
    const page = stripPages[anchor.index];
    if (page) scrollView.scrollTop = page.offsetTop + anchor.y * page.offsetHeight;
    scrollView.scrollLeft = Math.max(0, anchor.x * scrollView.scrollWidth - scrollView.clientWidth / 2);
  };

  const applyZoom = (preservePosition = true) => {
    const anchor = preservePosition ? captureZoomAnchor() : null;
    const targetWidth = Math.max(120, Math.round(fitWidth() * zoom));
    document.documentElement.style.setProperty("--reader-page-width", `${targetWidth}px`);
    if (readerStrip) readerStrip.style.width = `${Math.max(scrollView.clientWidth, targetWidth + 24)}px`;
    storage.set("mangabridge-reader-zoom-v3", zoom.toFixed(2));
    updateControls();
    if (anchor) window.requestAnimationFrame(() => restoreZoomAnchor(anchor));
  };
  applyZoomAfterLayout = applyZoom;

  const setZoom = (nextZoom) => {
    zoom = Math.max(0.5, Math.min(3, Math.round(nextZoom * 20) / 20));
    applyZoom(true);
  };

  const resetSingleViewport = (edge = "top") => {
    window.requestAnimationFrame(() => {
      pageFrame.scrollLeft = Math.max(0, (pageFrame.scrollWidth - pageFrame.clientWidth) / 2);
      pageFrame.scrollTop = edge === "bottom" ? Math.max(0, pageFrame.scrollHeight - pageFrame.clientHeight) : 0;
    });
  };

  const loadSinglePage = (index, persist = true, edge = "top") => {
    pageIndex = Math.max(0, Math.min(pageUrls.length - 1, index));
    const requestId = ++imageRequest;
    if (imageStatus) {
      imageStatus.hidden = false;
      const statusText = imageStatus.querySelector("span");
      if (statusText) statusText.textContent = "Loading page…";
    }
    singleImage.removeAttribute("src");
    singleImage.alt = `Chapter ${config.chapterNum}, page ${pageIndex + 1}`;
    const loader = new Image();
    loader.onload = () => {
      if (requestId !== imageRequest) return;
      singleImage.src = loader.src;
      applyZoom(false);
      resetSingleViewport(edge);
      if (imageStatus) imageStatus.hidden = true;
      preloadAround(pageIndex);
    };
    loader.onerror = () => {
      if (requestId !== imageRequest) return;
      if (imageStatus) {
        imageStatus.hidden = false;
        const statusText = imageStatus.querySelector("span");
        if (statusText) statusText.textContent = "Page failed to load. Click to retry.";
      }
    };
    loader.src = pageUrls[pageIndex];
    updateControls();
    if (persist) scheduleProgress(pageIndex);
    markLastPageRead();
  };

  if (imageStatus) imageStatus.addEventListener("click", () => loadSinglePage(pageIndex, false));

  const setMode = (nextMode) => {
    if (nextMode !== "single" && nextMode !== "scroll") return;
    if (mode === "scroll") pageIndex = visibleStripPage();
    mode = nextMode;
    storage.set("mangabridge-reader-mode-v3", mode);
    scrollView.hidden = mode !== "scroll";
    pageView.hidden = mode !== "single";
    if (mode === "single") {
      loadSinglePage(pageIndex, false, "top");
      window.requestAnimationFrame(() => pageFrame.focus({ preventScroll: true }));
    } else {
      applyZoom(false);
      window.requestAnimationFrame(() => {
        scrollToPage(pageIndex);
        scrollView.focus({ preventScroll: true });
      });
    }
    updateControls();
    scheduleProgress(pageIndex);
  };

  const changePage = (delta, edge = "top") => {
    if (mode !== "single") {
      pageIndex = visibleStripPage();
      const target = Math.max(0, Math.min(pageUrls.length - 1, pageIndex + delta));
      pageIndex = target;
      scrollToPage(target, "smooth");
      updateControls();
      scheduleProgress(target);
      return;
    }
    const target = Math.max(0, Math.min(pageUrls.length - 1, pageIndex + delta));
    if (target === pageIndex) return;
    loadSinglePage(target, true, edge);
  };

  const scrollSinglePage = (direction) => {
    const maxTop = Math.max(0, pageFrame.scrollHeight - pageFrame.clientHeight);
    const step = Math.max(120, pageFrame.clientHeight * 0.82);
    if (direction > 0) {
      if (pageFrame.scrollTop < maxTop - 4) {
        pageFrame.scrollBy({ top: step, behavior: "smooth" });
      } else {
        changePage(1, "top");
      }
    } else if (pageFrame.scrollTop > 4) {
      pageFrame.scrollBy({ top: -step, behavior: "smooth" });
    } else {
      changePage(-1, "bottom");
    }
  };

  let observer = null;
  if ("IntersectionObserver" in window) {
    observer = new IntersectionObserver((entries) => {
      if (mode !== "scroll") return;
      let selected = null;
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        if (!selected || entry.intersectionRatio > selected.intersectionRatio) selected = entry;
      });
      if (!selected) return;
      pageIndex = Number(selected.target.dataset.pageIndex || 0);
      updateControls();
      scheduleProgress(pageIndex);
      markLastPageRead();
    }, { root: scrollView, rootMargin: "-25% 0px -55% 0px", threshold: [0, 0.05, 0.2, 0.5] });
    stripPages.forEach((page) => observer.observe(page));
  } else {
    scrollView.addEventListener("scroll", () => {
      if (mode !== "scroll") return;
      pageIndex = visibleStripPage();
      updateControls();
      scheduleProgress(pageIndex);
      markLastPageRead();
    }, { passive: true });
  }

  if (modeButton) modeButton.addEventListener("click", () => setMode(mode === "single" ? "scroll" : "single"));
  if (previousPage) previousPage.addEventListener("click", () => changePage(-1));
  if (nextPage) nextPage.addEventListener("click", () => changePage(1));
  document.getElementById("page-hit-left")?.addEventListener("click", () => changePage(-1));
  document.getElementById("page-hit-right")?.addEventListener("click", () => changePage(1));
  if (firstPage) {
    firstPage.addEventListener("click", () => {
      pageIndex = 0;
      if (mode === "single") loadSinglePage(0, true, "top");
      else scrollToPage(0, "smooth");
    });
  }
  if (zoomOut) zoomOut.addEventListener("click", () => setZoom(zoom - 0.15));
  if (zoomIn) zoomIn.addEventListener("click", () => setZoom(zoom + 0.15));
  if (zoomReset) zoomReset.addEventListener("click", () => setZoom(1));

  if (markRead) {
    markRead.addEventListener("click", async () => {
      readState = !readState;
      updateReadButton();
      await saveProgress(readState ? "complete" : "unread", pageIndex);
    });
  }

  pageView.addEventListener("touchstart", (event) => {
    const touch = event.changedTouches[0];
    touchStartX = touch.clientX;
    touchStartY = touch.clientY;
  }, { passive: true });
  pageView.addEventListener("touchend", (event) => {
    if (touchStartX === null || touchStartY === null) return;
    const touch = event.changedTouches[0];
    const dx = touch.clientX - touchStartX;
    const dy = touch.clientY - touchStartY;
    touchStartX = touchStartY = null;
    const pageCanPanHorizontally = pageFrame.scrollWidth > pageFrame.clientWidth + 4;
    if (pageCanPanHorizontally || Math.abs(dx) < 55 || Math.abs(dx) < Math.abs(dy) * 1.25) return;
    changePage(dx < 0 ? 1 : -1);
  }, { passive: true });

  document.addEventListener("keydown", (event) => {
    if (event.target.matches("input, select, textarea")) return;
    const key = event.key.toLowerCase();
    if (key === "f") {
      event.preventDefault();
      setImmersive(!app.classList.contains("is-immersive"));
      return;
    }
    if (key === "m") {
      event.preventDefault();
      setMode(mode === "single" ? "scroll" : "single");
      return;
    }
    if (event.key === "Escape" && app.classList.contains("is-immersive")) {
      event.preventDefault();
      setImmersive(false);
      return;
    }
    if (event.key === "+" || event.key === "=") {
      event.preventDefault();
      setZoom(zoom + 0.15);
      return;
    }
    if (event.key === "-") {
      event.preventDefault();
      setZoom(zoom - 0.15);
      return;
    }
    if (event.key === "0") {
      event.preventDefault();
      setZoom(1);
      return;
    }
    if (mode === "single") {
      if (event.key === "ArrowLeft") { event.preventDefault(); changePage(-1); return; }
      if (event.key === "ArrowRight") { event.preventDefault(); changePage(1); return; }
      if (event.key === "PageUp" || (event.key === " " && event.shiftKey)) { event.preventDefault(); scrollSinglePage(-1); return; }
      if (event.key === "PageDown" || event.key === " ") { event.preventDefault(); scrollSinglePage(1); return; }
      if (event.key === "ArrowUp") { event.preventDefault(); pageFrame.scrollBy({ top: -80, behavior: "smooth" }); return; }
      if (event.key === "ArrowDown") { event.preventDefault(); pageFrame.scrollBy({ top: 80, behavior: "smooth" }); return; }
    }
    if (key === "[") { if (config.prevChapter) window.location.assign(config.prevChapter); return; }
    if (key === "]") { if (config.nextChapter) window.location.assign(config.nextChapter); }
  });

  window.addEventListener("resize", () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => applyZoom(true), 120);
  });

  updateReadButton();
  scrollView.hidden = mode !== "scroll";
  pageView.hidden = mode !== "single";
  applyZoom(false);
  if (mode === "single") loadSinglePage(pageIndex, false, "top");
  else window.requestAnimationFrame(() => scrollToPage(pageIndex));
  updateControls();
  saveProgress("open", pageIndex);
})();
