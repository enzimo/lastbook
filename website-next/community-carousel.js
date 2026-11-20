const carousel = document.querySelector(".release-carousel");
if (carousel) {
  const track = carousel.querySelector(".contribution-track");
  const controls = carousel.querySelector(".carousel-controls");
  const previous = controls.querySelector('[data-direction="-1"]');
  const next = controls.querySelector('[data-direction="1"]');
  const cards = Array.from(track.children);
  const releaseFilter = carousel.querySelector(".release-filter");
  const releaseNotes = carousel.querySelector(".release-notes-link");
  const releaseNotesText = releaseNotes.querySelector(".release-notes-text");
  const latestNotes = releaseNotes.href;
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");

  function updateControls() {
    const end = track.scrollWidth - track.clientWidth;
    previous.setAttribute("aria-disabled", String(track.scrollLeft < 2));
    next.setAttribute("aria-disabled", String(track.scrollLeft >= end - 2));
    controls.hidden = end < 2;
  }

  function move(direction) {
    const edge = track.getBoundingClientRect().left;
    const offsets = cards.filter((card) => !card.hidden).map((card) => card.getBoundingClientRect().left - edge);
    const offset = direction > 0
      ? offsets.find((distance) => distance > 2)
      : offsets.reverse().find((distance) => distance < -2);
    if (offset === undefined) return;
    track.scrollBy({left: offset, behavior: reducedMotion.matches ? "instant" : "smooth"});
  }

  releaseFilter.hidden = false;
  releaseFilter.addEventListener("change", () => {
    const version = releaseFilter.value;
    for (const card of cards) {
      card.hidden = version !== "" && card.dataset.release !== version;
    }
    releaseNotes.href = version ? new URL(`v${version}`, latestNotes).href : latestNotes;
    releaseNotesText.textContent = version ? "Release notes" : "Latest release notes";
    track.scrollTo({left: 0, behavior: "instant"});
    updateControls();
  });

  controls.addEventListener("click", (event) => {
    const button = event.target.closest("[data-direction]");
    if (button && button.getAttribute("aria-disabled") !== "true") {
      move(Number(button.dataset.direction));
    }
  });
  track.addEventListener("scroll", updateControls, {passive: true});
  track.addEventListener("keydown", (event) => {
    if (event.target !== track || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    move(event.key === "ArrowRight" ? 1 : -1);
  });
  new ResizeObserver(updateControls).observe(track);
  updateControls();
}
