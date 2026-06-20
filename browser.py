"""Long-lived stealth browser session for LinkedIn.

Holds one persistent cloakbrowser context and reuses it across all requests.
Playwright's sync API is bound to the thread that created the objects, and the
web server serves requests on other threads — so EVERY context operation runs
on a single dedicated worker thread (a max-1 ThreadPoolExecutor). The public
functions just submit work to that thread and block for the result. The login
session lives in PROFILE_DIR.

Login is a small state machine (see `login_state`). Startup tries to reuse the
on-disk session, else logs in with credentials. If LinkedIn answers with an
email/SMS confirmation code (it does this even with 2FA disabled), the challenge
page is kept open and the state becomes `awaiting_code`; the caller then feeds
the code via `submit_code`. Cookies for the live session are also captured into
memory (`cookies()`).
"""

import atexit
import os
import queue
import re
import shutil
import signal
import tempfile
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cloakbrowser as cb
from dotenv import load_dotenv

log = logging.getLogger("scraper.browser")

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = str(BASE_DIR / "profile")
STATE_FILE = str(BASE_DIR / "state.json")

# URL fragments that mean "not signed in".
WALL_MARKERS = ("/login", "/authwall", "/checkpoint", "/uas/")
# URL fragments that mean "LinkedIn is asking for a confirmation code / extra step".
CHALLENGE_MARKERS = ("/checkpoint", "/challenge")

# Candidate selectors for the confirmation-code input on the challenge page.
# LinkedIn rotates these, so we try several and fall back to any one-time-code /
# tel input inside the form.
PIN_INPUT_SELECTORS = (
    "input[name=pin]",
    "#input__email_verification_pin",
    "#input__phone_verification_pin",
    "input[autocomplete=one-time-code]",
    "input[name*=pin]",
    "input[id*=verification]",
    "input[type=tel]",
)
PIN_SUBMIT_SELECTORS = (
    "#email-pin-submit-button",
    "#two-step-submit-button",
    "button[type=submit]",
)

# LinkedIn's login page is now a React form with generated input IDs (the old
# #username / #password are gone) and a submit <button type="button"> with
# localized text (no <form>, so Enter won't submit). This tags the *visible*
# email field, password field, and credentials button with data-auto-login so
# the Python side can then drive real mouse/keyboard interactions (stealth).
# The submit button is the visible button with non-empty text that is NOT a
# social provider ("… com Apple"/"… with Google") nor the show-password icon,
# preferring one whose text reads like "sign in" across locales.
_TAG_LOGIN_JS = """() => {
  const vis = el => !!(el && el.offsetParent !== null);
  const pick = sels => { for (const s of sels) for (const el of document.querySelectorAll(s)) if (vis(el)) return el; return null; };
  const email = pick(['#username','input[name=session_key]','input[autocomplete~=username]','input[type=email]']);
  const password = pick(['#password','input[name=session_password]','input[autocomplete=current-password]','input[type=password]']);
  const PROVIDER = /apple|google|microsoft|facebook|with |com |continue|continuar/i;
  const SIGNIN = /sign in|entrar|log ?in|anmelden|connecter|inloggen|accedi|iniciar|\\u767b\\u5f55|\\ub85c\\uadf8\\uc778|\\u30ed\\u30b0\\u30a4\\u30f3/i;
  const btns = [...document.querySelectorAll('button, input[type=submit]')].filter(vis);
  let submit = btns.find(b => b.type === 'submit');
  if (!submit) {
    const cand = btns.filter(b => { const t = (b.innerText || b.value || '').trim(); return t && !PROVIDER.test(t); });
    submit = cand.find(b => SIGNIN.test(b.innerText || b.value || '')) || cand[0] || null;
  }
  if (email) email.setAttribute('data-auto-login', 'email');
  if (password) password.setAttribute('data-auto-login', 'password');
  if (submit) submit.setAttribute('data-auto-login', 'submit');
  return { email: !!email, password: !!password, submit: !!submit };
}"""

# Clean visible text of the main column. We strip two kinds of noise from the
# LIVE DOM before reading innerText:
#   - script/style/code/template/noscript: LinkedIn ships Voyager API JSON in
#     <code id="bpr-guid-*"> blobs that are sometimes rendered.
#   - footer / global nav / banner: LinkedIn's site-wide chrome (incl. the
#     ~40-language selector) that is identical on every page, so it would
#     otherwise repeat once per company section.
# Mutating the live page is safe: html = page.content() is captured BEFORE this
# runs and the page is closed straight after. innerText (not textContent) keeps
# it layout-aware so anything still display:none stays excluded too.
_CLEAN_TEXT_JS = """() => {
  const root = document.querySelector('main') || document.body;
  if (!root) return '';
  document.querySelectorAll(
    'script, style, code, template, noscript, footer, ' +
    '[role="banner"], [role="contentinfo"], #global-nav, .global-footer'
  ).forEach(n => n.remove());
  return root.innerText;
}"""

# Structured extraction. LinkedIn's React profile/company pages use fully hashed
# class names and NO <ul>/<li> for entries, so the only stable anchors are the
# section <h2> titles. For each section we keep its visible innerText (clean and
# well-ordered) plus any company/school/profile links it contains. This is
# deliberately resilient to the dynamic DOM — it never depends on a class name.
# Read-only: does not mutate the page, so it can run before _CLEAN_TEXT_JS.
_EXTRACT_JS = """() => {
  const norm = t => t.toLowerCase().replace(/&/g, ' and ')
      .replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  const main = document.querySelector('main') || document.body;
  const h1 = main.querySelector('h1');
  const entityLinks = el => [...new Set([...el.querySelectorAll('a[href]')]
      .map(a => (a.href || '').split('?')[0])
      .filter(h => /linkedin\\.com\\/(company|school|in)\\//.test(h)))];

  const topSec = h1 ? h1.closest('section') : main.querySelector('section');
  const top_card_lines = topSec
      ? topSec.innerText.split('\\n').map(s => s.trim()).filter(Boolean).slice(0, 16)
      : [];
  // The name is the <h1> when present; on some views (e.g. owner profile) it is
  // not an <h1>, so fall back to the first top-card line. Used to drop the
  // top-card wrapper, whose <h2> repeats the name, from the section list.
  let name = h1 ? h1.innerText.trim() : '';
  if (!name && top_card_lines.length) name = top_card_lines[0];

  const sections = {};
  const section_order = [];
  for (const sec of main.querySelectorAll('section')) {
    const h2 = sec.querySelector('h2');
    if (!h2) continue;
    const title = h2.innerText.split('\\n').map(s => s.trim()).filter(Boolean)[0];
    if (!title) continue;
    if (name && title === name) continue;       // skip the top-card / name wrapper
    const key = norm(title);
    if (!key || sections[key]) continue;         // first (outermost) occurrence wins
    let lines = sec.innerText.split('\\n').map(s => s.trim()).filter(Boolean)
        .filter((v, i, a) => v !== a[i - 1]);     // drop adjacent duplicate lines
    if (lines[0] === title) lines = lines.slice(1);
    sections[key] = { title, text: lines.join('\\n'), links: entityLinks(sec) };
    section_order.push(key);
  }
  return { name: name || null, top_card_lines, section_order, sections };
}"""

# Typed company data + posts, pulled from LinkedIn's authenticated Voyager API
# (same-origin fetch from the logged-in page; CSRF token read from the
# JSESSIONID cookie). This gives clean typed fields (companyId, locations,
# employeeCount, tagline, foundedOn, follower count, logo/cover image URLs, …)
# that the rendered DOM does not expose — far more reliable than scraping markup.
# Raw string: regex backslashes (\d, \/) are passed through to JS verbatim.
_COMPANY_API_JS = r"""async (slug) => {
  const csrf = (document.cookie.match(/JSESSIONID="?([^";]+)"?/) || [])[1];
  const H = { 'csrf-token': csrf, 'x-restli-protocol-version': '2.0.0', 'accept': 'application/json' };
  const imgUrl = (vec) => {
    const v = vec && vec.image && vec.image['com.linkedin.common.VectorImage'];
    if (!v || !v.artifacts || !v.artifacts.length) return null;
    const art = v.artifacts.reduce((a, b) => (b.width > a.width ? b : a));
    return v.rootUrl + art.fileIdentifyingUrlPathSegment;
  };
  const loc = (l) => l ? {
    country: l.country ?? null, city: l.city ?? null, geographicArea: l.geographicArea ?? null,
    postalCode: l.postalCode ?? null, line1: l.line1 ?? null, line2: l.line2 ?? null,
    description: l.description ?? null, headquarter: !!l.headquarter,
    localizedName: l.localizedName ?? l.city ?? null, latitude: l.latitude ?? null, longitude: l.longitude ?? null,
  } : null;
  const mapCompany = (c) => {
    const base = `https://www.linkedin.com/company/${c.universalName}`;
    const site = (c.companyPageUrl || (c.callToAction && c.callToAction.url) || '').replace(/^https?:\/\//, '').replace(/\/$/, '');
    const ind = (c.companyIndustries && c.companyIndustries[0] && c.companyIndustries[0].localizedName) || null;
    return {
      companyName: c.name ?? null,
      companyId: parseInt(((c.entityUrn || '').match(/(\d+)$/) || [])[1] || '0', 10) || null,
      locations: (c.confirmedLocations || []).map(loc),
      employeeCount: c.staffCount ?? null,
      callToAction: c.callToAction ? {
        displayText: c.callToAction.callToActionMessage && c.callToAction.callToActionMessage.text,
        type: c.callToAction.callToActionType, url: c.callToAction.url,
      } : null,
      croppedCoverImage: imgUrl(c.backgroundCoverImage),
      specialities: c.specialities || [],
      specialties: c.specialities || [],  // American spelling (consumer emits both)
      permalink: c.universalName || null, // LinkedIn shorthand slug (== universalName)
      crunchbaseFundingData: {},          // not available from Voyager; empty obj (consumer does .get(...,{}))
      employeeCountRange: c.staffCountRange ? { start: c.staffCountRange.start, end: c.staffCountRange.end } : null,
      tagline: c.tagline ?? null,
      followerCount: (c.followingInfo && c.followingInfo.followerCount) ?? null,
      originalCoverImage: imgUrl(c.backgroundCoverImage),
      logoResolutionResult: imgUrl(c.logo),
      industry: ind,
      description: c.description ?? null,
      websiteUrl: site || null,
      headquarter: c.headquarter ? {
        country: c.headquarter.country ?? null, city: c.headquarter.city ?? null,
        geographicArea: c.headquarter.geographicArea ?? null, postalCode: c.headquarter.postalCode ?? null,
        line1: c.headquarter.line1 ?? null, line2: c.headquarter.line2 ?? null, description: c.headquarter.description ?? null,
      } : null,
      foundedOn: c.foundedOn ? { month: c.foundedOn.month ?? null, year: c.foundedOn.year ?? null, day: c.foundedOn.day ?? null } : null,
      universalName: c.universalName ?? null,
      industryV2Taxonomy: ind,
      url: base + '/',
      affiliatedOrganizationsByEmployees: [],
      affiliatedOrganizationsByShowcases: [],
      organizationsUsingProduct: [],
    };
  };
  const mapPost = (el) => {
    const urn = (el.updateMetadata && el.updateMetadata.urn) || el.entityUrn || null;
    const act = (urn || '').match(/urn:li:activity:(\d+)/);
    const postUrl = act ? `https://www.linkedin.com/feed/update/urn:li:activity:${act[1]}/` : null;
    const cm = el.commentary && el.commentary.text;
    const text = cm && (cm.text || (typeof cm === 'string' ? cm : null));
    const s = (el.socialDetail && el.socialDetail.totalSocialActivityCounts) || {};
    const authorText = (el.actor && el.actor.name && el.actor.name.text) || null;
    const ago = ((el.actor && el.actor.subDescription && el.actor.subDescription.text) || '').trim() || null;
    // Try to derive an ISO date (YYYY-MM-DD) from the actor's "view activity"
    // navigation url (LinkedIn encodes a unix timestamp there); else null and
    // let the consumer fall back to `ago`.
    const nav = el.actor && el.actor.navigationContext && el.actor.navigationContext.url;
    const ts = nav && (nav.match(/fingerprint=(\d{10})/) || [])[1];
    let isoDate = null;
    if (ts) {
      const dt = new Date(parseInt(ts, 10) * 1000);
      if (!isNaN(dt)) isoDate = dt.toISOString().slice(0, 10);
    }
    // Kruncher-consumer field names (postText / reactionCount / commentsCount /
    // postAuthor{title} / ago / date / media). The raw urn/url/author are kept
    // as harmless pass-through extras; the consumer ignores unknown keys.
    return {
      postText: text || null,
      date: isoDate,
      ago: ago,
      reactionCount: s.numLikes ?? 0,
      commentsCount: s.numComments ?? 0,
      postAuthor: { title: authorText },
      media: [],
      // pass-through extras (not read by the consumer, useful for debugging):
      urn,
      url: postUrl,
      author: authorText,
      numShares: s.numShares ?? null,
    };
  };
  let company = null, error = null;
  try {
    const r = await fetch(`/voyager/api/organization/companies?decorationId=com.linkedin.voyager.deco.organization.web.WebFullCompanyMain-12&q=universalName&universalName=${encodeURIComponent(slug)}`, { headers: H, credentials: 'include' });
    if (r.ok) { const c = ((await r.json()).elements || [])[0]; if (c) company = mapCompany(c); }
    else error = 'company http ' + r.status;
  } catch (e) { error = String(e); }
  let posts = [];
  try {
    const r2 = await fetch(`/voyager/api/organization/updatesV2?q=companyFeedByUniversalName&companyUniversalName=${encodeURIComponent(slug)}&count=20`, { headers: H, credentials: 'include' });
    if (r2.ok) { const j = await r2.json(); posts = (j.elements || []).map(mapPost).filter(p => p.postText || p.url); }
  } catch (e) {}
  return { found: !!company, company, posts, error };
}"""

# Typed PROFILE data. LinkedIn deprecated the clean profile REST/GraphQL APIs
# (they return only references); the live profile page is Server-Driven UI, so
# the actual data is fetched from the SDUI component endpoints
# (/flagship-web/rsc-action/actions/component?componentId=...profileCards...).
# Those take the vanity slug directly (no URN), use a STABLE componentId (not a
# rotating queryId), and ride the session cookie + csrf-token. The response is
# React-Server-Component "flight" text; the visible values live in `children:["…"]`
# string nodes, which we reconstruct in document order and parse into sections.
# Top-card basics (name/headline/location) come from the page DOM (reliable in
# both visitor and owner views); the rich sections come from SDUI.
# Run on a profile page so the same-origin fetch carries cookies.
_PROFILE_API_JS = r"""async (vanityName) => {
  const csrf = (document.cookie.match(/JSESSIONID="?([^";]+)"?/) || [])[1];
  const parts = ["profileCardsAboveActivity","profileCardsBelowActivityPart1",
    "profileCardsBelowActivityPart2","profileCardsBelowActivityPart3",
    "profileCardsBelowActivityPart4","profileCardsBelowActivityPart5",
    "profileCardsBelowActivityPart6"];
  const body = JSON.stringify({clientArguments:{payload:{isSelfView:false,vanityName},
    states:[],requestMetadata:{"$type":"proto.sdui.common.RequestMetadata"},
    screenId:"com.linkedin.sdui.flagshipnav.home.Home"}});
  const NOISE = /^(Add |Show all|Show more|Show |See all|See |Edit|Enhance|Skip|Open to|Verifications|Endorse|Save to PDF|More$|Following$|Follow$|Connect$|Message$|Private to you$|Suggested for you$|Analytics$|Ask for a recommendation$|Give recommendation$|Send profile in a message$|…)/;
  const fetchLines = async (cid) => {
    try {
      const r = await fetch(`/flagship-web/rsc-action/actions/component?componentId=${cid}&sduiid=${cid}`,
        {method:"POST",headers:{"content-type":"application/json","csrf-token":csrf,"x-li-rsc-stream":"true","accept":"*/*"},credentials:"include",body});
      if(!r.ok) return [];
      const t = await r.text();
      return (t.match(/"children":\["[^"]{1,2000}"\]/g)||[])
        .map(s=>s.replace(/^"children":\["/,"").replace(/"\]$/,"").replace(/\\"/g,'"').replace(/\\n/g,"\n").trim())
        .filter(s=>s && !s.startsWith("$") && !NOISE.test(s))
        .filter((v,i,a)=>v!==a[i-1]);
    } catch(e){ return []; }
  };
  const pre = "com.linkedin.sdui.generated.profile.dsl.impl.";
  const all = []; const partEnd = [];
  for (const p of parts) { const ls = await fetchLines(pre+p); for (const l of ls) all.push(l); partEnd.push(all.length); }

  // --- top card from the DOM (reliable in visitor + owner views) ---
  const main = document.querySelector('main') || document.body;
  const h1 = main.querySelector('h1');
  const topSec = h1 ? h1.closest('section') : main.querySelector('section');
  const topLines = topSec ? topSec.innerText.split('\n').map(s=>s.trim()).filter(Boolean) : [];
  const name = (h1 && h1.innerText.trim()) || topLines[0] || null;
  // Drop the connection-degree badge ("· 1st"/"· 2nd"/pt-BR "· 1º"/"1st degree")
  // so it never becomes the headline — match the whole line to avoid stripping a
  // real title that happens to contain a number.
  const DEG = /^\s*[·•]?\s*\d+\s*(?:st|nd|rd|th|º|ª)\s*(?:degree|conexão|grau)?\s*$/i;
  const rest = topLines.filter(l=>l && l!==name && l!=='·' && !DEG.test(l));
  const headline = rest[0] || null;
  let location = null;
  for (const l of rest.slice(1)) {
    const low = l.toLowerCase();
    if (low.startsWith('contact')||low.startsWith('connect')||low.startsWith('message')||low.startsWith('follow')) break;
    if (l.includes(',')) { location = l; break; }
  }

  // --- section slicing over the reconstructed lines ---
  const SECTIONS=["About","Experience","Education","Licenses & certifications","Skills",
    "Recommendations","Interests","Volunteering","Projects","Courses","Languages",
    "Honors & awards","Organizations","Causes","Publications","Patents","Test scores"];
  // --- section slicing, TOC-aware -------------------------------------------
  // LinkedIn renders two layouts. (1) In-content headers: the section title sits
  // directly above its own entries. (2) TOC nav: a part lists several section
  // names CONSECUTIVELY (Experience, Education, Licenses, Projects, Volunteering)
  // then dumps ALL their entries in that order WITHOUT re-stating each header.
  // The old sliceSection only handled (1): for a TOC'd section it found the
  // header at its TOC spot, the next header was the adjacent TOC entry, and it
  // sliced EMPTY (the experience=[] regression). We now detect the TOC run and
  // partition the post-TOC content across those sections by section-type
  // boundary signals: Education ⇒ first YEAR-ONLY date span ("2011 – 2012"),
  // since Experience dates are month-year ("Feb 2025 - Present"); Licenses ⇒ a
  // line followed by "Issued …"; Projects ⇒ followed by "Associated with …".
  // In-content headers keep the old part-bounded next-header logic, so profiles
  // with no TOC are unchanged.
  const HDRSET=new Set(SECTIONS);
  const YEARDATE=/^\s*(19|20)\d\d\s*[–-]\s*(19|20)\d\d\s*$/;   // Education span
  const ranges=(()=>{
    const r={};
    // (a) in-content headers: hdr at i, content to the next header, part-bounded
    for(let i=0;i<all.length;i++){
      const h=all[i]; if(!HDRSET.has(h)||r[h]) continue;
      let pe=all.length; for(const pend of partEnd){ if(pend>i){ pe=pend; break; } }
      let e=pe; for(let j=i+1;j<pe;j++){ if(HDRSET.has(all[j])){ e=j; break; } }
      r[h]=[i+1,e];
    }
    // (b) detect the TOC: first run of ≥2 consecutive header lines, then
    // repartition its post-TOC content across the listed sections in order.
    let tocStart=-1,tocEnd=-1;
    for(let i=0;i<all.length;i++){
      if(!HDRSET.has(all[i])) continue;
      let j=i; while(j<all.length && HDRSET.has(all[j])) j++;
      if(j-i>=2){ tocStart=i; tocEnd=j; break; }
    }
    if(tocStart>=0){
      const order=all.slice(tocStart,tocEnd).filter(x=>HDRSET.has(x));
      let pe=all.length; for(const pend of partEnd){ if(pend>tocStart){ pe=pend; break; } }
      let ce=pe; for(let j=tocEnd;j<pe;j++){ if(HDRSET.has(all[j])){ ce=j; break; } }
      const content=all.slice(tocEnd,ce);
      // start index within `content` for each section, scanning forward from the
      // previous boundary; a section whose detector fails gets [] (its content
      // folds into the previous detected section — raw_sections loses nothing).
      const findNear=(re,win,from)=>{ for(let i=from;i<content.length;i++){ for(let d=i+1;d<=i+win&&d<content.length;d++){ if(re.test(content[d])) return i; } } return -1; };
      const starts=[0]; let cur=0;
      for(let k=1;k<order.length;k++){
        const sec=order[k]; let s=-1;
        if(sec==="Education") s=findNear(YEARDATE,2,cur);
        else if(sec==="Licenses & certifications") s=findNear(/^Issued/,2,cur);
        else if(sec==="Projects") s=findNear(/^Associated with/,4,cur);
        if(s<0) s=content.length;
        starts.push(s); if(s<content.length) cur=s;
      }
      starts.push(content.length);
      for(let k=0;k<order.length;k++) r[order[k]]=[tocEnd+starts[k], tocEnd+starts[k+1]];
    }
    return r;
  })();
  const sliceSection=(hdr)=>{ const rg=ranges[hdr]; return rg? all.slice(rg[0],rg[1]) : null; };

  const DUR=/^\d+\s+(yr|yrs|mo|mos|year|years|month|months)(\s+\d+\s+(mo|mos))?$/;
  const ETYPE=/^(Full-time|Part-time|Freelance|Self-employed|Contract|Internship|Apprenticeship|Seasonal|Temporary)$/;
  const DATE=/(Present|\b(19|20)\d\d\b).*[-–].*(Present|\b(19|20)\d\d\b)|[A-Z][a-z]{2}\s+\d{4}\s*[-–]/;
  const YEAR=/^\s*(19|20)\d\d\s*[-–]\s*((19|20)\d\d|Present)\s*$/;
  // Strong location keywords used only to disambiguate a 2-line grouped role
  // ([location, title]) from a single position ([title, company]). Comma-style
  // locations ("Palo Alto, California") are handled by line-count in the single
  // branch, so they are intentionally NOT here (a comma alone is ambiguous with
  // titles like "Founding LP, Investment Committee").
  const LOC=/\b(Remote|Hybrid|On-?site)\b|\bArea\b|^Greater\s|Metropolitan|\bRegion\b/;
  const okLoc=(l)=> !!l && l.length<=60 && !/[.!?]$/.test(l);

  // EXPERIENCE: a single date-anchored parser that handles every layout seen in
  // the wild — single roles (Title, Company, Date, [Location]), grouped/promotion
  // blocks (Company, Duration, [Location], [Title, Type, Date]xN), and mixes of
  // the two. Each position is anchored on its date-range line; the lead-in lines
  // before it give title/company (company inherited from a grouped block header),
  // and a location attaches to the position it follows.
  const exp = sliceSection("Experience") || [];
  const experience=[];
  (function(){
    const dateIdx=[]; for(let k=0;k<exp.length;k++) if(DATE.test(exp[k])) dateIdx.push(k);
    let blockCompany=null, prev=-1;
    const setPrevLoc=(l)=>{ if(okLoc(l)&&experience.length&&!experience[experience.length-1].location) experience[experience.length-1].location=l; };
    for(const d of dateIdx){
      const seg=exp.slice(prev+1,d);
      let headerIdx=-1; for(let k=0;k<seg.length-1;k++) if(DUR.test(seg[k+1])) headerIdx=k;
      let title=null, company=null, curLoc=null, etype=null;
      if(headerIdx>=0){                               // grouped-block company header
        if(headerIdx>0) setPrevLoc(seg[headerIdx-1]);
        company=blockCompany=seg[headerIdx];
        const after=seg.slice(headerIdx+2).filter(x=>!DUR.test(x)&&!DATE.test(x));
        etype=after.find(x=>ETYPE.test(x))||null;
        const nt=after.filter(x=>!ETYPE.test(x));
        title=nt[nt.length-1]||null; if(nt.length>1&&okLoc(nt[0])) curLoc=nt[0];
      } else {
        const nd=seg.filter(x=>!DUR.test(x));
        etype=nd.find(x=>ETYPE.test(x))||null;
        const nt=nd.filter(x=>!ETYPE.test(x));
        if(blockCompany && (nt.length===1 || (nt.length===2 && LOC.test(nt[0])))){  // continuing a grouped block
          title=nt[nt.length-1]||null; company=blockCompany; if(nt.length>1) setPrevLoc(nt[0]);
        } else {                                       // single position (ends any block)
          blockCompany=null; company=nt[nt.length-1]||null; title=nt[nt.length-2]||null; if(nt.length>2) setPrevLoc(nt[0]);
        }
      }
      experience.push({company,title,employmentType:etype,dateRange:exp[d],location:curLoc});
      prev=d;
    }
    if(dateIdx.length){ const tail=exp.slice(dateIdx[dateIdx.length-1]+1).filter(x=>!DUR.test(x)&&!ETYPE.test(x)&&!DATE.test(x)); if(tail.length) setPrevLoc(tail[0]); }
  })();

  // EDUCATION: [school, optional degree, optional dates]. Date detection covers
  // year-spans, month-year spans, and single years. Long paragraphs (>140 chars)
  // are skipped — they are role/"…more" descriptions that the SDUI streams at the
  // end of the part and would otherwise be mistaken for schools (the full text is
  // still in raw_sections). "+N" "show more" markers are skipped too.
  const isEduDate=(x)=> DATE.test(x) || /^\s*(19|20)\d\d\s*$/.test(x);
  const edu = sliceSection("Education") || [];
  const education=[]; let ei=0;
  // A school name is short and never ends with sentence punctuation; anything
  // longer/sentence-like is a streamed description, so skip it as an entry start.
  const notSchool=(s)=> s.length>90 || /[.!?]$/.test(s) || /^\+\d+/.test(s);
  while(ei<edu.length){
    const ln=edu[ei];
    if(notSchool(ln)){ ei++; continue; }
    const e={school:ln, degree:null, dates:null}; let j=ei+1;
    if(edu[j]&&!isEduDate(edu[j])&&edu[j].length<=140){ e.degree=edu[j]; j++; }
    if(edu[j]&&isEduDate(edu[j])){ e.dates=edu[j]; j++; }
    education.push(e); ei=Math.max(j,ei+1);
  }

  // LICENSES: name, issuer, issued date
  const lic = sliceSection("Licenses & certifications") || [];
  const licenses=[]; let li=0;
  while(li<lic.length){
    const o={name:lic[li], issuer:null, issued:null}; let j=li+1;
    if(lic[j]&&!/^Issued|^Credential/.test(lic[j])&&lic[j].length<=140){ o.issuer=lic[j]; j++; }
    if(lic[j]&&/^Issued/.test(lic[j])){ o.issued=lic[j]; j++; }
    if(lic[j]&&/^Credential/.test(lic[j])){ j++; }
    licenses.push(o); li=Math.max(j,li+1);
  }

  // about: the long summary isn't in the SDUI children nodes — read it from the
  // DOM About section (longest line = the summary paragraph).
  let about = null;
  const aboutSec = [...main.querySelectorAll('section')].find(s=>{const h=s.querySelector('h2');return h&&h.innerText.trim().split('\n')[0]==='About';});
  if (aboutSec){ const ls=aboutSec.innerText.split('\n').map(s=>s.trim()).filter(Boolean).filter(l=>l!=='About'&&l!=='Top skills'); about=ls.sort((a,b)=>b.length-a.length)[0]||null; }

  // skills: full Skills section if present, else the "Top skills" line (• joined).
  let skills = (sliceSection("Skills")||[]).filter(s=>!/^Endorsed|endorsement/i.test(s));
  if (!skills.length){ const tl=all.find(l=>l.includes(" • ")&&/[A-Za-z]/.test(l)); if(tl) skills=tl.split(" • ").map(s=>s.trim()).filter(Boolean); }

  // other sections kept as cleaned lines
  const sections={};
  for(const h of ["Recommendations","Interests","Volunteering","Projects","Courses","Languages","Honors & awards","Organizations","Publications"]){
    const ls=sliceSection(h); if(ls&&ls.length) sections[h.toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_+|_+$/g,'')]={title:h, lines:ls};
  }

  // RAW FALLBACK: the complete ordered lines of EVERY section. The typed fields
  // above are best-effort; this guarantees nothing is ever lost, even on a layout
  // the typed parsers have never seen — a consumer (or an LLM pass) can always
  // recover the full content from here.
  const key=(h)=>h.toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_+|_+$/g,'');
  const raw_sections={};
  for(const h of SECTIONS){ const ls=sliceSection(h); if(ls&&ls.length) raw_sections[key(h)]=ls; }

  return { found: experience.length>0 || education.length>0 || !!about || all.length>0,
           name, headline, location, about, experience, education, licenses, skills,
           sections, raw_sections, section_headers: SECTIONS.filter(s=>all.includes(s)) };
}"""

# Every LOGIN/AUTH call (start_login, submit_code, reset, is_logged_in,
# close) runs on this single worker thread — they all touch the ONE persistent
# context that owns ./profile. Data fetches (fetch_*) used to share it too, but
# now route through an opt-in scrape POOL (see _ScrapePool below) when
# SCRAPE_POOL_SIZE >= 2; at the default (1) they still run here, identical to
# the old single-worker behavior.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cloakbrowser")
_ctx = None  # created and used only inside the login worker thread

# --- login state machine -----------------------------------------------------
# States: idle | logging_in | awaiting_code | logged_in | no_credentials | failed
_login_state = "idle"
_login_detail = ""
_pending_page = None  # the open challenge page, kept alive between login + code
_cookies = None       # in-memory cookies of the live session (list of dicts)
_cookie_gen = 0       # bumped on every cookie snapshot → pool workers re-seed
_state_lock = threading.Lock()

# --- circuit breaker (#8) ---------------------------------------------------
# When the session dies, EVERY fetch lands on a /login|/authwall|/checkpoint URL.
# Counting consecutive wall-hits lets us trip a breaker: once open, NEW scrapes
# fail fast (→ HTTP 409) instead of each one burning a full ~60s goto to discover
# the session is dead, and a background re-login is kicked. Cached data STILL
# flows while open (it's valid, just possibly stale) — so a transient blip barely
# affects callers. Auto-resets after CB_COOLDOWN_SECONDS so a successful re-login
# re-enables traffic.
_CB_THRESHOLD = max(1, int(os.environ.get("CB_THRESHOLD", "3") or "3"))
_CB_COOLDOWN = float(os.environ.get("CB_COOLDOWN_SECONDS", "60") or "60")
_wall_hits = 0
_cb_open = False
_cb_open_until = 0.0  # monotonic timestamp after which to try one request again


class SessionDegradedError(RuntimeError):
    """Circuit breaker is open (session looks dead + re-login in flight) → HTTP 409.
    Callers should retry after a few seconds (cached results still succeed)."""


def breaker_state() -> dict:
    """Breaker snapshot for /health. Safe from any thread."""
    now = time.monotonic()
    with _state_lock:
        open_now = _cb_open and now < _cb_open_until
        return {
            "open": open_now,
            "wall_hits": _wall_hits,
            "threshold": _CB_THRESHOLD,
        }


def _record_wall(final_url: str) -> None:
    """Called by a fetch impl when the page landed on an auth-wall URL. Bumps the
    consecutive-hit counter and trips the breaker at the threshold (spawning a
    background re-login). Idempotent while already open."""
    global _wall_hits, _cb_open, _cb_open_until
    trip = False
    with _state_lock:
        _wall_hits += 1
        if _wall_hits >= _CB_THRESHOLD and not (_cb_open and time.monotonic() < _cb_open_until):
            _cb_open = True
            _cb_open_until = time.monotonic() + _CB_COOLDOWN
            trip = True
    if trip:
        log.warning("circuit breaker OPEN after %d wall-hits — fast-failing new scrapes + re-login", _wall_hits)
        threading.Thread(target=start_login, name="breaker-relogin", daemon=True).start()


def _record_authed() -> None:
    """Called by a fetch impl on a clean, signed-in result. Resets the breaker."""
    global _wall_hits, _cb_open
    with _state_lock:
        if _wall_hits or _cb_open:
            _wall_hits = 0
            _cb_open = False


def _check_breaker() -> None:
    """Raise SessionDegradedError if the breaker is open AND within cooldown.
    After the cooldown we let ONE request through (half-open): if it walls
    again _record_wall re-opens it; if it succeeds _record_authed closes it."""
    global _cb_open
    with _state_lock:
        if _cb_open and time.monotonic() < _cb_open_until:
            raise SessionDegradedError(
                f"session degraded (re-login in flight); retry in a few seconds"
            )
        # cooldown elapsed → half-open: allow this request to probe the session
        if _cb_open:
            _cb_open = False


def _set_state(state: str, detail: str = "") -> None:
    global _login_state, _login_detail
    with _state_lock:
        _login_state = state
        _login_detail = detail
    log.info("login state -> %s (%s)", state, detail)


def login_state() -> dict:
    """Current login state. Safe to call from any thread (no browser I/O)."""
    with _state_lock:
        return {
            "state": _login_state,
            "detail": _login_detail,
            "logged_in": _login_state == "logged_in",
        }


def cookies():
    """In-memory cookies of the live session, or None if not logged in yet."""
    with _state_lock:
        return _cookies


# Browser viewport. cloakbrowser's default is {1920, 947}, but 947px is too SHORT
# for a long LinkedIn profile: LinkedIn lazy-renders lower sections (Skills,
# Recommendations, Languages...) only if they fit in the viewport, so at 947px
# those sections NEVER render no matter how much you scroll — the page reports
# scrollHeight==innerHeight and there's nothing to scroll. A taller viewport
# (2400px) makes the full profile render immediately (Skills visible in ~1s,
# no scroll gymnastics needed). This is the fix for the "skills missing ~40% of
# the time" flakiness; it is NOT a stealth signal (real screens are this tall).
_VIEWPORT = {"width": 1920, "height": 2400}


def _launch_args():
    """Extra Chromium flags. In a container, Chromium can't use its sandbox
    (running as root / no user-namespaces) and /dev/shm is tiny, so set
    CHROMIUM_NO_SANDBOX=true there. These are launch flags, invisible to pages,
    so they don't weaken stealth."""
    if os.environ.get("CHROMIUM_NO_SANDBOX", "").lower() in ("1", "true", "yes"):
        return ["--no-sandbox", "--disable-dev-shm-usage"]
    return None


def _reap_chromium(user_data_dir) -> int:
    """SIGKILL every Chromium process launched with `user_data_dir` as its
    --user-data-dir. The path is unique per context (a fresh mkdtemp for each
    pool worker; PROFILE_DIR for the login context), so matching it inside the
    process cmdline is precise — it cannot hit another browser (desktop Chrome,
    other containers). Returns the number of PIDs killed.

    This is the ONLY reliable way to reclaim a wedged context: Playwright's
    ctx.close() can hang indefinitely when the browser is stuck, but the OS
    reaps a SIGKILL'd process at once — and killing the browser also UNBLOCKS
    the worker thread stuck inside a Playwright call (it then raises and dies).
    Without it, every deadline breach / hung shutdown leaks a whole Chromium
    (~150-300MB) and the box OOMs under bursts. Linux-only (/proc); no-ops
    elsewhere."""
    if not user_data_dir or not os.path.isdir("/proc"):
        return 0
    marker = str(user_data_dir)
    me = os.getpid()
    killed = 0
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "ignore")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if marker not in cmd:
            continue
        # Safety: only kill Chromium-class binaries, never an unrelated process
        # that merely happens to cite the path in its cmdline.
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            exe = ""
        if "chrom" not in exe.lower():
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    return killed


def _clear_profile_locks():
    """Remove stale Chromium singleton locks from the profile.

    If the process is killed (e.g. a container restart) Chromium leaves
    SingletonLock/Socket/Cookie behind, pointing at the old host+PID. On the next
    launch Chromium then refuses to reuse the profile ("profile appears to be in
    use by another Chromium process") and exits with code 21. Clearing them is
    safe here because only one Chromium ever uses this profile (single worker).
    """
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = Path(PROFILE_DIR) / name
        try:
            if p.is_symlink() or p.exists():
                p.unlink()
        except OSError:
            pass


def _get_context():
    """Lazily create and cache the persistent (headless) browser context."""
    global _ctx
    if _ctx is None:
        _clear_profile_locks()
        _ctx = cb.launch_persistent_context(
            PROFILE_DIR,
            headless=True,
            humanize=True,
            viewport=_VIEWPORT,
            args=_launch_args(),
        )
    return _ctx


# Thread-local "current context". On the login worker thread it's unset, so
# _current_ctx() falls back to the shared persistent context (_get_context()).
# On a scrape-pool worker thread the worker pins its own ephemeral context here
# (see _ScrapeWorker), so the same _fetch_*_impl runs unchanged on either thread.
_worker_ctx = threading.local()


def _current_ctx():
    """The context this thread should drive: a pool worker's pinned context, or
    the shared persistent login context on the login worker thread."""
    c = getattr(_worker_ctx, "ctx", None)
    return c if c is not None else _get_context()


def _authed(url: str) -> bool:
    """True if a LinkedIn URL is a signed-in page (not a login/authwall)."""
    return "linkedin.com" in url and not any(m in url for m in WALL_MARKERS)


def _session_ok(ctx) -> bool:
    """Probe the feed on the given context; True if it stays on a signed-in page."""
    page = ctx.new_page()
    try:
        page.goto(
            "https://www.linkedin.com/feed/",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        return _authed(page.url)
    finally:
        page.close()


def _capture_cookies(ctx) -> None:
    """Snapshot the context cookies into memory (best-effort) and bump the
    cookie generation so scrape-pool workers know to re-seed their ephemeral
    contexts with the fresh session."""
    global _cookies, _cookie_gen
    try:
        snap = ctx.cookies()
    except Exception:  # noqa: BLE001 - cookies are a convenience, not critical
        snap = None
    with _state_lock:
        _cookies = snap
        _cookie_gen += 1


def _find_pin_input(page):
    """Return (selector, element) for the first visible confirmation-code input."""
    for sel in PIN_INPUT_SELECTORS:
        try:
            el = page.query_selector(sel)
        except Exception:  # noqa: BLE001
            el = None
        if el:
            try:
                if el.is_visible():
                    return sel, el
            except Exception:  # noqa: BLE001
                return sel, el
    return None, None


def _fill_login_form(page, email: str, password: str) -> None:
    """Type credentials into LinkedIn's (React, dynamic-ID) login form and submit.

    Waits for the email field to render, tags the visible email/password/submit
    elements in-page, then drives real mouse/keyboard interactions. Raises if the
    form can't be found (LinkedIn served something unexpected).
    """
    try:
        page.wait_for_selector(
            "#username, input[type=email], input[autocomplete~=username]",
            state="visible",
            timeout=30_000,
        )
    except Exception:  # noqa: BLE001 - the tag step below makes the final call
        pass
    tagged = page.evaluate(_TAG_LOGIN_JS)
    if not (tagged and tagged.get("email") and tagged.get("password")):
        raise RuntimeError(
            "login form not found (LinkedIn markup changed) — "
            f"tagged={tagged}, url={page.url}"
        )
    page.click("[data-auto-login=email]")
    page.type("[data-auto-login=email]", email, delay=60)
    time.sleep(0.4)
    page.type("[data-auto-login=password]", password, delay=60)
    time.sleep(0.3)
    if tagged.get("submit"):
        page.click("[data-auto-login=submit]")
    else:
        page.keyboard.press("Enter")


def _start_login_impl() -> None:
    """Establish a session, or park at `awaiting_code` if LinkedIn challenges us.

    Fully headless. Fast path: reuse a valid ./profile session. Otherwise log in
    with LINKEDIN_EMAIL / LINKEDIN_PASSWORD. If LinkedIn responds with an
    email/SMS confirmation code (a /checkpoint with a PIN field — it asks for
    this even when 2FA is off), the challenge page is kept open and the state
    becomes `awaiting_code` so a code can be submitted via submit_code().
    """
    global _pending_page
    _set_state("logging_in")
    load_dotenv()
    try:
        ctx = _get_context()
        reused = _session_ok(ctx)
    except Exception as exc:  # noqa: BLE001 - surface launch/probe failures
        _set_state("failed", f"browser launch/probe failed: {exc}")
        return

    if reused:
        _capture_cookies(ctx)
        _set_state("logged_in", "reused existing session")
        return

    email = os.environ.get("LINKEDIN_EMAIL")
    password = os.environ.get("LINKEDIN_PASSWORD")
    if not (email and password):
        _set_state(
            "no_credentials",
            "no valid session and LINKEDIN_EMAIL / LINKEDIN_PASSWORD are unset",
        )
        return

    log.info("no session — logging in headless with credentials")
    page = ctx.new_page()
    try:
        page.goto(
            "https://www.linkedin.com/login",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        _fill_login_form(page, email, password)
        try:
            page.wait_for_url(
                lambda u: _authed(u) or any(m in u for m in CHALLENGE_MARKERS),
                timeout=45_000,
            )
        except Exception:  # noqa: BLE001 - the URL check below decides the outcome
            pass
        time.sleep(2)
        url = page.url

        if _authed(url):
            ctx.storage_state(path=STATE_FILE)
            _capture_cookies(ctx)
            page.close()
            _set_state("logged_in", "password login")
            return

        sel, _ = _find_pin_input(page)
        if sel or any(m in url for m in CHALLENGE_MARKERS):
            _pending_page = page  # keep open; submit_code() will type the code
            _set_state(
                "awaiting_code",
                f"confirmation code required (url: {url})",
            )
            return

        page.close()
        _set_state("failed", f"unexpected post-login url: {url}")
    except Exception as exc:  # noqa: BLE001
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass
        _set_state("failed", f"login error: {exc}")


def _submit_code_impl(code: str) -> dict:
    """Type the confirmation code into the parked challenge page and verify.

    On success the session is persisted, cookies are captured, and the state
    flips to logged_in. A rejected/expired code leaves the page open in
    awaiting_code so the caller can retry.
    """
    global _pending_page
    with _state_lock:
        state = _login_state
    if state != "awaiting_code" or _pending_page is None:
        return {"ok": False, "state": state, "error": f"not awaiting a code (state={state})"}

    page = _pending_page
    sel, el = _find_pin_input(page)
    if not sel:
        return {
            "ok": False,
            "state": "awaiting_code",
            "error": "no confirmation-code input found on the challenge page",
            "detail": page.url,
        }

    try:
        page.fill(sel, code)
        time.sleep(0.3)
        clicked = False
        for bsel in PIN_SUBMIT_SELECTORS:
            btn = page.query_selector(bsel)
            if btn:
                btn.click()
                clicked = True
                break
        if not clicked:
            page.keyboard.press("Enter")

        try:
            page.wait_for_url(lambda u: _authed(u), timeout=45_000)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
        url = page.url

        if _authed(url) and not any(m in url for m in CHALLENGE_MARKERS):
            ctx = _get_context()
            ctx.storage_state(path=STATE_FILE)
            _capture_cookies(ctx)
            page.close()
            _pending_page = None
            _set_state("logged_in", "verified with confirmation code")
            return {"ok": True, "state": "logged_in"}

        _set_state("awaiting_code", f"code not accepted (url: {url})")
        return {
            "ok": False,
            "state": "awaiting_code",
            "error": "code not accepted (still on the challenge page)",
            "detail": url,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "state": "awaiting_code", "error": f"submit error: {exc}"}


def _reset_impl() -> None:
    """Tear everything down: close the context + any parked challenge page, drop
    in-memory cookies, and delete the on-disk session (./profile + state.json) so
    the next login is a true cold start (which is what triggers LinkedIn's
    confirmation-code challenge). Leaves the state at `idle`.
    """
    global _ctx, _pending_page, _cookies
    log.info("resetting session: closing context and clearing %s", PROFILE_DIR)
    if _pending_page is not None:
        try:
            _pending_page.close()
        except Exception:  # noqa: BLE001
            pass
        _pending_page = None
    if _ctx is not None:
        try:
            _ctx.close()
        except Exception:  # noqa: BLE001
            pass
        _ctx = None
    with _state_lock:
        _cookies = None
    _cache_clear()  # results were fetched under the now-wiped session
    for path in (PROFILE_DIR, STATE_FILE):
        try:
            p = Path(path)
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        except Exception as exc:  # noqa: BLE001
            log.warning("reset: could not remove %s: %s", path, exc)
    _set_state("idle", "session reset")


# --- adaptive scroll tuning ------------------------------------------------
# LinkedIn lazy-renders lower sections (Education, Skills, Recommendations…)
# only once scrolled into view, so we loop until the page height stops growing.
# These knobs make that loop ADAPTIVE instead of a fixed tax:
#   - GROW_POLL:   short wait between probes WHILE the page is still growing —
#                 lets us descend long pages quickly (the old code always paid
#                 0.9s per step, even during active loading).
#   - CONFIRM_POLL: longer wait used for the final stability probes, once the
#                  height first looks settled, so a slow lazy load that lands
#                  shortly after still has time to grow the page (and reset the
#                  stable counter) before we declare done.
#   - STABLE_NEED: how many consecutive unchanged probes settle the page.
#                  2 × CONFIRM_POLL = ~1.2s grace, enough for typical lazy loads.
#   - MAX_ITERS:   hard cap so a pathological page can never hang the worker.
# Bounded by iteration count (not wall-clock) so unit tests that stub out
# time.sleep still terminate. Native time.sleep, NOT page.wait_for_timeout
# (the latter emits CDP signals that anti-bot checks detect).
_SCROLL_GROW_POLL = 0.25
_SCROLL_CONFIRM_POLL = 0.6
_SCROLL_STABLE_NEED = 2
_SCROLL_MAX_ITERS = 60


def _scroll_all(page) -> None:
    """Scroll to the bottom to trigger every lazy-loaded section, then back up.

    Adaptive: polls fast while the page is still growing (cheap descent), then
    takes a couple of slower confirmation probes before declaring it settled —
    so a finished page isn't taxed by the old fixed 0.9s-per-step loop, while a
    slow lazy section still gets a chance to land. NOTE: this only works reliably
    because the viewport is tall (see _VIEWPORT) — LinkedIn won't render lower
    sections unless they fit in the viewport, so a short viewport makes the page
    report scrollHeight==innerHeight and there's nothing to scroll at all.
    """
    last_h = 0
    stable = 0
    for _ in range(_SCROLL_MAX_ITERS):
        page.mouse.wheel(0, 1400)
        # Fast probe while content is still loading; slow down to confirm once
        # the height first appears unchanged, so late lazy loads aren't missed.
        time.sleep(_SCROLL_GROW_POLL if stable == 0 else _SCROLL_CONFIRM_POLL)
        try:
            h = page.evaluate("() => document.body.scrollHeight")
        except Exception:  # noqa: BLE001
            break
        if h == last_h:
            stable += 1
            if stable >= _SCROLL_STABLE_NEED:
                break
        else:
            stable = 0
            last_h = h
    page.mouse.wheel(0, -2_000_000)
    time.sleep(0.4)


def _fetch_html_impl(url: str) -> dict:
    """Open URL in the logged-in session, render it, return a result dict.

    Returns {requested_url, final_url, title, html, text, name, headline,
    location, top_card_lines, sections}. final_url/title let the caller see if
    LinkedIn redirected (e.g. to /login, /authwall, or an overlay) rather than
    serving the page that was asked for.

    Uses _current_ctx(): the persistent login context on the login worker, or a
    scrape-pool worker's own ephemeral context on a pool thread — so this fn is
    agnostic to which thread/context it runs on. Each call opens + closes its
    own page, so concurrent calls on different contexts are safe.
    """
    ctx = _current_ctx()
    page = ctx.new_page()
    try:
        log.info("goto requested_url=%s", url)
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        _scroll_all(page)

        html = page.content()
        final_url = page.url
        title = page.title()

        # Structured sections (read-only) BEFORE the destructive clean-text pass.
        try:
            extracted = page.evaluate(_EXTRACT_JS)
        except Exception:  # noqa: BLE001
            extracted = {"name": None, "top_card_lines": [], "section_order": [], "sections": {}}

        # Clean text = the rendered VISIBLE text of the main column. innerText
        # skips hidden nodes, so LinkedIn's embedded Voyager API JSON (hidden
        # <code> blobs) is excluded — far cleaner than an extractor on the HTML.
        try:
            text = page.evaluate(_CLEAN_TEXT_JS)
        except Exception:  # noqa: BLE001 - fall back to None if eval fails
            text = None

        if final_url.rstrip("/") != url.rstrip("/"):
            log.warning("redirected requested_url=%s final_url=%s", url, final_url)
        if any(marker in final_url for marker in WALL_MARKERS):
            log.warning("auth/checkpoint wall hit: final_url=%s", final_url)
            _record_wall(final_url)   # feed the circuit breaker
        else:
            _record_authed()           # a clean signed-in page resets it

        top_lines = extracted.get("top_card_lines") or []
        headline, location = _top_card_fields(top_lines, extracted.get("name"))
        log.info(
            "fetched final_url=%s title=%r html_bytes=%d sections=%s",
            final_url,
            title,
            len(html),
            ",".join(extracted.get("section_order") or []),
        )
        return {
            "requested_url": url,
            "final_url": final_url,
            "title": title,
            "html": html,
            "text": text,
            "name": extracted.get("name"),
            "headline": headline,
            "location": location,
            "top_card_lines": top_lines,
            "sections": extracted.get("sections") or {},
        }
    finally:
        page.close()


# A connection-degree badge rendered in the top card ("· 1st", "· 2nd", pt-BR
# "· 1º", or "1st degree") must not become the headline — it's not the person's
# title. Match the WHOLE line so we never strip a real headline containing a #.
_DEGREE_BADGE_RE = re.compile(
    r"^\s*[·•]?\s*\d+\s*(?:st|nd|rd|th|º|ª)\s*(?:degree|conexão|grau)?\s*$",
    re.IGNORECASE,
)


def _top_card_fields(lines: list, name) -> tuple:
    """Best-effort headline + location from the top-card text lines."""
    rest = [
        l for l in lines
        if l and l != name and l.strip() != "·"
        and not _DEGREE_BADGE_RE.match(l.strip())
    ]
    headline = rest[0] if rest else None
    location = None
    for l in rest[1:]:
        low = l.lower()
        if low.startswith(("contact", "connect", "message", "follow", "·")):
            break
        if "," in l:  # "City, Region, Country" — a reliable location shape
            location = l
            break
    return headline, location


def _is_logged_in_impl() -> bool:
    """Probe whether the persisted session is still authenticated.

    Visits the feed; LinkedIn bounces unauthenticated visitors to /login or
    /authwall, so a final URL still on the feed means we're logged in. Runs on
    the login worker thread against the persistent context.
    """
    ctx = _get_context()
    page = ctx.new_page()
    try:
        page.goto(
            "https://www.linkedin.com/feed/",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        final = page.url
        logged_in = "/login" not in final and "/authwall" not in final
        log.info("health probe final_url=%s logged_in=%s", final, logged_in)
        return logged_in
    finally:
        page.close()


# Company sub-tabs, in display order. "home" is the base URL (no segment).
COMPANY_SECTIONS = ("about", "posts", "jobs", "people", "insights", "life")


def _company_slug(url: str):
    """Return the company slug from a /company/<slug>/... URL, else None."""
    m = re.search(r"/company/([^/?#]+)", url)
    return m.group(1) if m else None


def _profile_slug(url: str):
    """Return the vanity slug from a /in/<slug>/... URL, else None."""
    m = re.search(r"/in/([^/?#]+)", url)
    return m.group(1) if m else None


def _discover_sections(html: str, slug: str) -> list:
    """Which company sub-tabs exist, read from the rendered nav links."""
    found = set(re.findall(rf"/company/{re.escape(slug)}/([a-z_]+)/", html))
    return [s for s in COMPANY_SECTIONS if s in found]


def _fetch_company_impl(url: str) -> dict:
    """Scrape every section of a company page (home + about/posts/jobs/...).

    Runs on one worker thread and calls _fetch_html_impl directly (never the
    public wrapper) so it does not re-enter the pool / deadlock. Each sub-section
    is still strictly sequential on that one context (read via _current_ctx).
    """
    slug = _company_slug(url)
    if not slug:
        # not a company URL — fall back to a single page under "page"
        return {"base_url": url, "slug": None, "sections": {"page": _fetch_html_impl(url)}}

    base = f"https://www.linkedin.com/company/{slug}/"
    log.info("full company scrape slug=%s", slug)
    sections = {"home": _fetch_html_impl(base)}
    for seg in _discover_sections(sections["home"]["html"], slug):
        log.info("scraping section: %s", seg)
        sections[seg] = _fetch_html_impl(f"{base}{seg}/")
    log.info("company scrape done: %s", ", ".join(sections))
    return {"base_url": base, "slug": slug, "sections": sections}


def _fetch_company_api_impl(url: str) -> dict:
    """Typed company data + posts via the authenticated Voyager API.

    Navigates to the company page only to be on the linkedin.com origin (so the
    same-origin /voyager fetch sends cookies), then runs _COMPANY_API_JS. Returns
    {slug, found, company, posts, final_url, error}.
    """
    slug = _company_slug(url)
    if not slug:
        raise RuntimeError(f"not a company URL: {url}")
    ctx = _current_ctx()
    page = ctx.new_page()
    try:
        page.goto(
            f"https://www.linkedin.com/company/{slug}/",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        # Feed the circuit breaker: if the session died the goto bounces to a
        # wall URL; a clean company page resets it.
        if any(m in page.url for m in WALL_MARKERS):
            _record_wall(page.url)
        else:
            _record_authed()
        # LinkedIn's SPA client-navigates right after the initial load, which
        # destroys the JS execution context mid-evaluate ("Execution context
        # was destroyed … because of a navigation"). Let it settle, then retry
        # the same-origin Voyager fetch a few times until the context is stable.
        data = None
        last_exc = None
        for attempt in range(4):
            time.sleep(2)
            try:
                data = page.evaluate(_COMPANY_API_JS, slug)
                break
            except Exception as exc:  # noqa: BLE001 - retry navigation races
                last_exc = exc
                log.warning("company evaluate attempt %d failed: %s", attempt + 1, exc)
        if data is None:
            raise RuntimeError(f"company evaluate failed after retries: {last_exc}")
        data["slug"] = slug
        data["final_url"] = page.url
        log.info(
            "company api slug=%s found=%s posts=%d error=%s",
            slug,
            data.get("found"),
            len(data.get("posts") or []),
            data.get("error"),
        )
        return data
    finally:
        page.close()


def _fetch_profile_api_impl(url: str) -> dict:
    """Typed profile data via the authenticated SDUI component endpoints.

    Navigates to the profile page (to be on the linkedin.com origin and to read
    the top-card from the DOM), then runs _PROFILE_API_JS which fetches the
    profileCards SDUI components and parses them into typed sections. Returns
    {slug, found, name, headline, location, about, experience, education,
    licenses, skills, sections, final_url}.
    """
    slug = _profile_slug(url)
    if not slug:
        raise RuntimeError(f"not a profile URL: {url}")
    ctx = _current_ctx()
    page = ctx.new_page()
    try:
        page.goto(
            f"https://www.linkedin.com/in/{slug}/",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        # Feed the circuit breaker (same as the company path).
        if any(m in page.url for m in WALL_MARKERS):
            _record_wall(page.url)
        else:
            _record_authed()
        # Same SPA-navigation race as the company path: settle, then retry.
        data = None
        last_exc = None
        for attempt in range(4):
            time.sleep(2)
            try:
                data = page.evaluate(_PROFILE_API_JS, slug)
                break
            except Exception as exc:  # noqa: BLE001 - retry navigation races
                last_exc = exc
                log.warning("profile evaluate attempt %d failed: %s", attempt + 1, exc)
        if data is None:
            raise RuntimeError(f"profile evaluate failed after retries: {last_exc}")
        data["slug"] = slug
        data["final_url"] = page.url
        log.info(
            "profile api slug=%s found=%s exp=%d edu=%d",
            slug,
            data.get("found"),
            len(data.get("experience") or []),
            len(data.get("education") or []),
        )
        return data
    finally:
        page.close()


# --- scrape pool (opt-in parallelism) ---------------------------------------
# When SCRAPE_POOL_SIZE >= 2, data fetches (fetch_*) run on N parallel cookie-
# seeded ephemeral contexts instead of serializing on the single login worker.
# Each pool worker owns one thread + one persistent TEMP profile (the same stealth
# launch path as the login context, so the fingerprint matches) and re-seeds its
# cookies whenever the session changes (_cookie_gen). Default 1 = exact legacy
# single-worker behavior (login + scrapes share the one persistent context,
# strictly serialized) — so tests and the deployed box are unchanged until this
# is explicitly enabled.
#
# SCRAPE_QUEUE_TIMEOUT: seconds a request waits for a free worker before raising
# ScrapeBusyError (→ HTTP 503 + Retry-After). 0 = wait forever (the old behavior).
#
# SCRAPE_DEADLINE_SECONDS: HARD wall-clock cap on a single scrape. If the browser
# op hasn't returned by then the caller gets ScrapeDeadlineError (→ HTTP 504) FAST
# and the pool worker is poisoned + replaced. This is the guard that prevents a
# wedged browser (thrash, stuck IPC, hung page.evaluate/content) from holding a
# slot FOREVER — without it one stuck request can hang the whole worker until the
# process is killed. Set generously (a normal /extract is ~5-15s, full company
# ~3min); the default 180 covers a full company scrape with headroom.
#
# ANTI-BOT CAUTION: more concurrent navigations from one IP raises LinkedIn's
# scrutiny. Start at 2 and watch the logs for /checkpoint before raising it.
_SCRAPE_POOL_SIZE = max(1, int(os.environ.get("SCRAPE_POOL_SIZE", "1") or "1"))
_SCRAPE_QUEUE_TIMEOUT = float(os.environ.get("SCRAPE_QUEUE_TIMEOUT", "0") or "0")
_SCRAPE_DEADLINE_SECONDS = float(os.environ.get("SCRAPE_DEADLINE_SECONDS", "180") or "180")
# Recycle a worker's Chromium after this many requests to shed in-process memory
# growth (a Chromium that has done many heavy navigations bloats; this caps it by
# closing + relaunching on the next acquire). 0 = never recycle.
_SCRAPE_WORKER_MAX_REQUESTS = int(os.environ.get("SCRAPE_WORKER_MAX_REQUESTS", "40") or "40")


class ScrapeBusyError(RuntimeError):
    """All scrape workers are busy and the queue timeout elapsed (→ HTTP 503)."""


class ScrapeDeadlineError(RuntimeError):
    """A scrape exceeded SCRAPE_DEADLINE_SECONDS (→ HTTP 504). The worker it ran on
    is poisoned and will be replaced, so a wedged browser can't hold a slot forever."""


class _ScrapeWorker:
    """One thread + one persistent temp-profile context, cookie-seeded from the
    global snapshot. All context ops happen on its own single-thread executor
    (Playwright sync objects are thread-bound), so several of these can run in
    parallel safely."""

    def __init__(self, name: str):
        self._name = name
        self._ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self._ctx = None
        self._profile_dir: str | None = None
        self._seeded_gen = None
        self._poisoned = False  # set when a scrape blew the deadline → replace me
        self._n_requests = 0   # for memory-growth recycling (_SCRAPE_WORKER_MAX_REQUESTS)

    def _hard_reap(self):
        """SIGKILL this worker's Chromium by its profile dir. Safe from ANY thread
        (uses /proc, not the Playwright objects). Guarantees the OS process is
        gone even if ctx.close() would hang; killing the browser also unblocks a
        worker thread wedged inside a Playwright call."""
        if self._profile_dir:
            n = _reap_chromium(self._profile_dir)
            if n:
                log.info("reaped %d chromium procs from %s", n, self._name)

    def _ensure(self):
        # runs on this worker's thread
        # Recycle a long-lived context to shed in-process memory growth: a
        # Chromium that has done many heavy navigations bloats, so after N
        # requests we kill + relaunch it (the previous request already
        # completed, so the thread is NOT wedged here — a clean recycle).
        if (
            self._ctx is not None
            and _SCRAPE_WORKER_MAX_REQUESTS > 0
            and self._n_requests >= _SCRAPE_WORKER_MAX_REQUESTS
        ):
            log.info("recycling %s after %d requests", self._name, self._n_requests)
            old = self._profile_dir
            self._hard_reap()
            self._ctx = None
            self._n_requests = 0
            if old:
                shutil.rmtree(old, ignore_errors=True)
            self._profile_dir = None
        with _state_lock:
            cookies = _cookies
            gen = _cookie_gen
        if self._ctx is None:
            self._profile_dir = tempfile.mkdtemp(prefix="cb-scrape-")
            self._ctx = cb.launch_persistent_context(
                self._profile_dir,
                headless=True,
                humanize=True,
                viewport=_VIEWPORT,
                args=_launch_args(),
            )
            self._seeded_gen = None
        # (re)seed when the live session changed since we last seeded this ctx.
        if gen != self._seeded_gen:
            if cookies:
                try:
                    self._ctx.clear_cookies()
                except Exception:  # noqa: BLE001
                    pass
                self._ctx.add_cookies(cookies)
            self._seeded_gen = gen
        # Pin this context as the thread's "current" one so the shared
        # _fetch_*_impl (which call _current_ctx()) drive THIS context here.
        _worker_ctx.ctx = self._ctx
        return self._ctx

    def run(self, fn, deadline: float = 0.0):
        """Run fn() on this worker's thread, capped at `deadline` seconds of
        wall-clock. On timeout: SIGKILL the wedged Chromium (so it can't leak),
        poison this worker (the pool replaces it), and raise ScrapeDeadlineError
        so the caller gets a fast 504 instead of hanging forever."""
        from concurrent.futures import TimeoutError as _FutTimeout
        fut = self._ex.submit(lambda: (self._ensure(), fn())[1])
        try:
            if deadline and deadline > 0:
                result = fut.result(timeout=deadline)
            else:
                result = fut.result()
            self._n_requests += 1
            return result
        except _FutTimeout:
            self._poisoned = True
            # Reap the wedged browser NOW (not when the pool swaps us out) so a
            # burst of deadline breaches can't pile up orphaned Chromiums → OOM.
            self._hard_reap()
            self._ex.shutdown(wait=False, cancel_futures=True)
            log.warning(
                "scrape worker %s blew the %.0fs deadline — reaped chromium + poisoned",
                self._name, deadline,
            )
            raise ScrapeDeadlineError(
                f"scrape exceeded {deadline:.0f}s deadline (worker {self._name} replaced)"
            )

    def close(self):
        """Bounded graceful close, then a GUARANTEED force-reap: try ctx.close()
        for at most 5s (it flushes cookies + lets the browser exit cleanly), but
        whether or not it returns, SIGKILL the Chromium by profile dir so the
        process is always gone — a hung close can never strand an orphan."""
        def _graceful():
            if self._ctx is not None:
                try:
                    self._ctx.close()
                except Exception:  # noqa: BLE001
                    pass
                self._ctx = None
        try:
            self._ex.submit(_graceful).result(timeout=5)
        except Exception:  # noqa: BLE001 - timeout/error → fall through to reap
            pass
        self._hard_reap()
        self._ex.shutdown(wait=False, cancel_futures=True)
        if self._profile_dir:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
            self._profile_dir = None


class _ScrapePool:
    """A bounded pool of _ScrapeWorkers. Acquire-a-worker is a FIFO token queue,
    so overflow naturally blocks (backpressure) or raises ScrapeBusyError once the
    queue timeout elapses — that's what turns a client pile-on into clean 503s."""

    def __init__(self, size: int):
        self._size = size
        self._workers = [_ScrapeWorker(f"cb-scrape-{i}") for i in range(size)]
        self._free = queue.Queue()
        self._replaced = 0
        self._waiting = 0  # requests blocked waiting for a free slot (→ /health queue_depth)
        self._waiting_lock = threading.Lock()
        for i in range(size):
            self._free.put(i)

    def run(self, fn, timeout: float = 0.0, deadline: float = 0.0):
        with self._waiting_lock:
            self._waiting += 1
        try:
            if timeout and timeout > 0:
                try:
                    idx = self._free.get(timeout=timeout)
                except queue.Empty:
                    raise ScrapeBusyError("all scrape workers busy")
            else:
                idx = self._free.get()  # block forever (legacy behavior)
        finally:
            with self._waiting_lock:
                self._waiting -= 1
        try:
            worker = self._workers[idx]
            # If the previous request on this slot blew its deadline, the worker
            # already SIGKILL'd its own Chromium (in run()) and is flagged
            # poisoned — swap in a fresh worker so the slot is healthy again.
            # (The wedged thread is left to die on its own; it holds nothing the
            # new worker needs.)
            if worker._poisoned:
                log.warning("replacing poisoned scrape worker at slot %d", idx)
                worker = _ScrapeWorker(f"{worker._name}-r{self._replaced}")
                self._workers[idx] = worker
                self._replaced += 1
            return worker.run(fn, deadline=deadline)
        finally:
            self._free.put(idx)

    @property
    def in_flight(self) -> int:
        return self._size - self._free.qsize()

    def close(self):
        for w in self._workers:
            w.close()


_scrape_pool = _ScrapePool(_SCRAPE_POOL_SIZE) if _SCRAPE_POOL_SIZE >= 2 else None


def scrape_stats() -> dict:
    """Snapshot of scrape-pool load for /health, so callers/LBs can self-throttle."""
    if _scrape_pool is None:
        return {"enabled": False, "size": 1, "in_flight": 0, "queue_depth": 0}
    return {
        "enabled": True,
        "size": _SCRAPE_POOL_SIZE,
        "in_flight": _scrape_pool.in_flight,
        "queue_depth": _scrape_pool._waiting,  # noqa: SLF001 - exposed for ops
    }


def _close_scrape_pool() -> None:
    """Tear down every pool worker's context + temp profile (shutdown path)."""
    if _scrape_pool is not None:
        _scrape_pool.close()


def _reap_all_chromium() -> None:
    """SIGKILL every Chromium we spawned — the login context (PROFILE_DIR) plus
    each pool worker's temp profile — regardless of whether graceful close ran.
    Defense-in-depth: registered via atexit and called at the end of
    close_context(), so a wedged browser can never leave orphaned processes
    behind (the OOM cause). Safe to call repeatedly; no-ops if nothing matches."""
    try:
        _reap_chromium(PROFILE_DIR)
        pool = _scrape_pool
        if pool is not None:
            for w in pool._workers:  # noqa: SLF001 - shutdown-only access
                if w._profile_dir:
                    _reap_chromium(w._profile_dir)
    except Exception:  # noqa: BLE001 - never raise from an atexit/shutdown hook
        pass


atexit.register(_reap_all_chromium)


# --- public API: login/auth on the single login worker; data fetches via _scrape

def _run(fn, *args):
    return _executor.submit(fn, *args).result()


# --- result cache + single-flight coalescing (#1, #2) ----------------------
# LinkedIn profile/company data is stable for hours, so repeat hits to the same
# URL should be ~free instead of another full browser scrape. Two layers:
#   1. TTL cache: (endpoint, url) -> result, with a per-endpoint TTL. Profiles
#      ~1h, companies ~6h, /extract ~1h. Bypassed with force=True (?force=1).
#   2. Single-flight: N concurrent requests for the SAME url do ONE scrape; the
#      rest wait on the leader's result and share it (good for stealth — fewer
#      parallel hits — AND throughput). On success the leader fills the cache.
# Cleared on reset_login (the session the data was fetched under is gone).
_CACHE_TTL = {
    "extract":      float(os.environ.get("CACHE_TTL_EXTRACT", "3600") or "3600"),      # /extract (DOM)
    "extract_full": float(os.environ.get("CACHE_TTL_EXTRACT_FULL", "3600") or "3600"), # /extract company full
    "company":      float(os.environ.get("CACHE_TTL_COMPANY", "21600") or "21600"),    # /company (Voyager)
    "profile":      float(os.environ.get("CACHE_TTL_PROFILE", "3600") or "3600"),      # /profile (SDUI)
}
_CACHE_MAX = int(os.environ.get("CACHE_MAX_ENTRIES", "512") or "512")  # bound memory
_CACHE_ENABLED = os.environ.get("CACHE_DISABLED", "").lower() not in ("1", "true", "yes")

_cache: dict = {}            # key -> (expires_monotonic, value)
_cache_lock = threading.Lock()
_inflight: dict = {}         # key -> _Inflight (single-flight coordinator)
_inflight_lock = threading.Lock()
_runtime = {
    "cache_hits": 0, "cache_misses": 0, "cache_coalesced": 0, "cache_size": 0,
    "last_scrape_ok": None, "last_scrape_at": None, "consecutive_failures": 0,
}
_runtime_lock = threading.Lock()
_MISS = object()


class _Inflight:
    """Single-flight handle: the leader does the scrape, followers wait on the
    event and re-share the result/exception."""
    __slots__ = ("event", "result", "exc", "leader")

    def __init__(self, leader: bool):
        self.event = threading.Event()
        self.result = None
        self.exc = None
        self.leader = leader


def _cache_get(key: str):
    if not _CACHE_ENABLED:
        return _MISS
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry[0] > now:
            return entry[1]
    return _MISS


def _cache_put(key: str, value, ttl: float) -> None:
    if not _CACHE_ENABLED or ttl <= 0:
        return
    with _cache_lock:
        _cache[key] = (time.monotonic() + ttl, value)
        # bound size: drop the soonest-expiring entries when over capacity
        over = len(_cache) - _CACHE_MAX
        if over > 0:
            for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:over]:
                _cache.pop(k, None)


def _cache_clear() -> None:
    with _cache_lock:
        _cache.clear()


def _mark_scrape(ok: bool) -> None:
    with _runtime_lock:
        _runtime["last_scrape_at"] = time.time()
        _runtime["last_scrape_ok"] = ok
        if ok:
            _runtime["consecutive_failures"] = 0
        else:
            _runtime["consecutive_failures"] += 1
        _runtime["cache_size"] = len(_cache)


def runtime_stats() -> dict:
    """Operational snapshot for /health (#9): cache efficacy + recent scrape
    health, so callers/LBs can self-throttle instead of guessing."""
    with _runtime_lock:
        r = dict(_runtime)
    r["cache_enabled"] = _CACHE_ENABLED
    r["cache_size"] = len(_cache)
    return r


def _cached_scrape(key: str, impl, url: str, ttl: float, force: bool = False):
    """Cache + single-flight wrapper around _scrape. Returns the cached result
    on a hit; on a miss, exactly ONE caller (the leader) runs the scrape while
    concurrent same-key callers wait and share its result. Raises ScrapeBusyError
    (→503) / ScrapeDeadlineError (→504) from the underlying scrape on failure."""
    cache_key = f"{key}:{url}"
    # 1. cache hit? (cached data STILL flows while the breaker is open — it's
    # valid, just possibly stale — so a transient re-login barely affects callers)
    if not force:
        v = _cache_get(cache_key)
        if v is not _MISS:
            with _runtime_lock:
                _runtime["cache_hits"] += 1
            _mark_scrape(True)
            return v
    # 2. breaker: if the session looks dead, fail fast (→ 409) instead of every
    # queued request burning a full goto to rediscover it. (Cache hits above
    # already bypassed this.)
    _check_breaker()
    # 3. single-flight: become leader OR attach as a follower
    with _inflight_lock:
        inflight = _inflight.get(cache_key)
        if inflight is None:
            inflight = _Inflight(leader=True)
            _inflight[cache_key] = inflight
        else:
            inflight = _Inflight(leader=False)
    with _runtime_lock:
        _runtime["cache_misses"] += 1
    if inflight.leader:
        leader_entry = _inflight[cache_key]
        try:
            result = _scrape(impl, url)
            _cache_put(cache_key, result, ttl)
            leader_entry.result = result
            _mark_scrape(True)
            return result
        except BaseException as exc:  # noqa: BLE001 - share failure with followers too
            leader_entry.exc = exc
            _mark_scrape(False)
            raise
        finally:
            leader_entry.event.set()
            with _inflight_lock:
                _inflight.pop(cache_key, None)
    else:
        # follower: wait for the leader's scrape and re-share it (no browser work).
        with _runtime_lock:
            _runtime["cache_coalesced"] += 1
        real = _inflight.get(cache_key)
        if real is None:
            # leader finished + cleaned up before we attached: just recurse once
            return _cached_scrape(key, impl, url, ttl, force=force)
        real.event.wait()
        if real.exc is not None:
            raise real.exc
        _mark_scrape(True)
        return real.result


def _scrape(impl, url):
    """Run a data-fetch impl(url). Routes to the scrape pool when enabled
    (SCRAPE_POOL_SIZE>=2) or, by default, to the single login worker with the
    persistent context — the exact legacy path. Raises ScrapeBusyError (→503) if
    the bounded pool queue is full, or ScrapeDeadlineError (→504) if the work
    blows SCRAPE_DEADLINE_SECONDS (so a wedged browser can never hang a caller)."""
    if _scrape_pool is None:
        # Single login worker: still cap it so a stuck op returns 504 fast. We
        # do NOT poison here (the login worker owns ./profile and can't be
        # swapped); its own goto/eval timeouts will eventually unblock it.
        from concurrent.futures import TimeoutError as _FutTimeout
        fut = _executor.submit(impl, url)
        try:
            return fut.result(timeout=_SCRAPE_DEADLINE_SECONDS)
        except _FutTimeout:
            raise ScrapeDeadlineError(
                f"scrape exceeded {_SCRAPE_DEADLINE_SECONDS:.0f}s deadline"
            )
    return _scrape_pool.run(
        lambda: impl(url),
        timeout=_SCRAPE_QUEUE_TIMEOUT,
        deadline=_SCRAPE_DEADLINE_SECONDS,
    )


def start_login() -> dict:
    """Attempt login on the worker thread. Returns the resulting login_state()."""
    _run(_start_login_impl)
    return login_state()


def submit_code(code: str) -> dict:
    return _run(_submit_code_impl, code)


def reset_login() -> dict:
    """Wipe the session (context + ./profile + cookies) so login starts fresh.
    Returns the post-reset state (`idle`); callers re-trigger start_login()."""
    _run(_reset_impl)
    return login_state()


def _close_impl() -> None:
    """Gracefully close the context (flushes Chromium cookies to the profile)."""
    global _ctx, _pending_page
    if _pending_page is not None:
        try:
            _pending_page.close()
        except Exception:  # noqa: BLE001
            pass
        _pending_page = None
    if _ctx is not None:
        try:
            _ctx.close()
        except Exception:  # noqa: BLE001
            pass
        _ctx = None


def close_context() -> None:
    """Bounded graceful close of the login context + scrape pool, then a
    GUARANTEED force-reap of ALL our Chromium. A wedged browser can't hang
    shutdown (every step is timeout-bounded) and can never strand orphaned
    processes (the final reap SIGKILLs anything still alive by profile dir)."""
    try:
        _executor.submit(_close_impl).result(timeout=5)
    except Exception:  # noqa: BLE001 - timeout → fall through to force-reap
        pass
    _close_scrape_pool()
    _reap_all_chromium()


def fetch_html(url: str, force: bool = False) -> dict:
    return _cached_scrape("extract", _fetch_html_impl, url, _CACHE_TTL["extract"], force)


def fetch_company(url: str, force: bool = False) -> dict:
    return _cached_scrape("extract_full", _fetch_company_impl, url, _CACHE_TTL["extract_full"], force)


def fetch_company_api(url: str, force: bool = False) -> dict:
    return _cached_scrape("company", _fetch_company_api_impl, url, _CACHE_TTL["company"], force)


def fetch_profile_api(url: str, force: bool = False) -> dict:
    return _cached_scrape("profile", _fetch_profile_api_impl, url, _CACHE_TTL["profile"], force)


def is_logged_in() -> bool:
    return _run(_is_logged_in_impl)
