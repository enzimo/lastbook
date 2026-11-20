const promptField = document.getElementById("contribution-prompt");
const copyPrompt = document.getElementById("copy-contribution-prompt");
const copyStatus = document.getElementById("copy-prompt-status");

copyPrompt.hidden = false;
copyPrompt.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(promptField.value);
    copyStatus.textContent = "Copied.";
  } catch {
    promptField.focus();
    promptField.select();
    copyStatus.textContent = "Prompt selected. Press ⌘C or Ctrl+C to copy.";
  }
});

promptField.addEventListener("input", () => {
  copyStatus.textContent = "";
});
