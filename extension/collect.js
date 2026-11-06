// Reads what's visible on the page the user is looking at. Runs only when the
// user clicks "Save to CRM" - nothing is collected in the background.
// Selectors use TikTok's data-e2e test hooks, which change less often than class names.
(function (root) {
  const RESERVED_IG = new Set(["p", "reel", "reels", "stories", "explore", "accounts", "direct", "tv"]);

  function text(doc, sel) {
    const el = doc.querySelector(sel);
    const t = el && el.textContent ? el.textContent.trim() : "";
    return t || null;
  }

  function meta(doc, name) {
    const el = doc.querySelector(`meta[property="${name}"], meta[name="${name}"]`);
    return el ? el.getAttribute("content") : null;
  }

  // TikTok wraps bio links as /link/v2?...&target=<real url>
  function unwrapLink(href) {
    if (!href) return null;
    try {
      const u = new URL(href, "https://www.tiktok.com");
      if (u.hostname.endsWith("tiktok.com") && u.pathname.startsWith("/link")) {
        return u.searchParams.get("target") || null;
      }
      return u.href;
    } catch {
      return /^[\w.-]+\.[a-z]{2,}(\/\S*)?$/i.test(href) ? "https://" + href : null;
    }
  }

  function tiktok(loc, doc) {
    const m = loc.pathname.match(/^\/@([\w.-]+)(?:\/(video|photo)\/(\d+))?/);
    if (!m) return null;
    const data = { platform: "tiktok", handle: m[1], url: `https://www.tiktok.com/@${m[1]}` };
    data.display_name = text(doc, '[data-e2e="user-subtitle"]') || text(doc, '[data-e2e="user-title"]');
    data.bio = text(doc, '[data-e2e="user-bio"]');
    const link = doc.querySelector('[data-e2e="user-link"]');
    data.bio_link = link ? unwrapLink(link.getAttribute("href") || link.textContent.trim()) : null;
    data.followers = text(doc, '[data-e2e="followers-count"]');
    if (m[2]) {
      data.video_url = `https://www.tiktok.com/@${m[1]}/${m[2]}/${m[3]}`;
      data.caption = text(doc, '[data-e2e="browse-video-desc"]') || text(doc, '[data-e2e="video-desc"]') || meta(doc, "og:description");
      data.sound = text(doc, '[data-e2e="browse-music"]') || text(doc, '[data-e2e="video-music"]');
    }
    return data;
  }

  function instagram(loc, doc) {
    const seg = loc.pathname.split("/").filter(Boolean);
    const title = meta(doc, "og:title") || doc.title || "";
    const fromTitle = title.match(/^(.*?)\s*\(@([\w.]+)\)/);
    let handle = null;
    if (seg.length && !RESERVED_IG.has(seg[0])) handle = seg[0];
    else if (fromTitle) handle = fromTitle[2];
    const data = { platform: "instagram", url: handle ? `https://www.instagram.com/${handle}/` : loc.href, handle };
    if (fromTitle && fromTitle[1]) data.display_name = fromTitle[1].replace(/ on Instagram.*$/, "").trim() || null;
    const desc = meta(doc, "og:description") || "";
    const followers = desc.match(/([\d.,]+[KkMm]?)\s+Followers/);
    if (followers) data.followers = followers[1];
    if (seg[0] === "p" || seg[0] === "reel" || seg[0] === "reels") data.video_url = loc.href.split("?")[0];
    return data;
  }

  function youtube(loc, doc) {
    const seg = loc.pathname.split("/").filter(Boolean);
    const data = { platform: "youtube" };
    if (seg[0] && (seg[0].startsWith("@") || seg[0] === "channel" || seg[0] === "c" || seg[0] === "user")) {
      data.url = `https://www.youtube.com/${seg.slice(0, seg[0].startsWith("@") ? 1 : 2).join("/")}`;
      data.display_name = text(doc, "#channel-name #text") || text(doc, "yt-dynamic-text-view-model h1") || meta(doc, "og:title");
      data.bio = meta(doc, "og:description");
      return data;
    }
    if (seg[0] === "watch" || seg[0] === "shorts") {
      const owner = doc.querySelector('ytd-video-owner-renderer a[href^="/@"], #owner a[href^="/@"], ytd-channel-name a[href]');
      if (!owner) return null;
      data.url = new URL(owner.getAttribute("href"), "https://www.youtube.com").href;
      data.display_name = owner.textContent.trim() || null;
      data.video_url = loc.href;
      data.caption = (doc.title || "").replace(/ - YouTube$/, "") || null;
      return data;
    }
    return null;
  }

  function collect(loc, doc) {
    const host = loc.hostname;
    let data = null;
    if (host.endsWith("tiktok.com")) data = tiktok(loc, doc);
    else if (host.endsWith("instagram.com")) data = instagram(loc, doc);
    else if (host.endsWith("youtube.com")) data = youtube(loc, doc);
    if (!data || !data.url) return null;
    for (const k of Object.keys(data)) if (data[k] == null || data[k] === "") delete data[k];
    return data;
  }

  const api = { collect, unwrapLink };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PressEngineCollect = api;
})(typeof window !== "undefined" ? window : globalThis);
