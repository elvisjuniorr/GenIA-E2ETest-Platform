(() => {
  const root = (window.GenIA = window.GenIA || {});
  const { authService } = root.auth;

  const setupProfilePage = () => {
    const refreshProfile = () => {
      const user = authService.getCurrentUser();
      if (!user) return;

      const firstName = String(user.firstName || "").trim();
      const lastName = String(user.lastName || "").trim();
      const fullName = [firstName, lastName].filter(Boolean).join(" ") || "—";
      const initials = [firstName, lastName]
        .filter(Boolean)
        .map((part) => part.charAt(0).toLocaleUpperCase("pt-BR"))
        .join("") || "?";
      const birthDate = user.birthDate
        ? new Date(`${user.birthDate}T00:00:00`).toLocaleDateString("pt-BR")
        : "—";

      document.getElementById("profileInitials").textContent = initials;
      document.getElementById("profileName").textContent = fullName;
      document.getElementById("profileEmail").textContent = user.email || "—";
      document.getElementById("profileBirthDate").textContent = birthDate;
    };

    refreshProfile();
    return { refreshProfile };
  };

  root.features = root.features || {};
  root.features.profile = { setupProfilePage };
})();
