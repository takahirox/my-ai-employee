document.querySelector("#toggle").addEventListener("click", () => {
  const status = document.querySelector("#status");
  status.textContent = "Ready";
  status.classList.add("ready");
  console.log("fixture-ready");
});
