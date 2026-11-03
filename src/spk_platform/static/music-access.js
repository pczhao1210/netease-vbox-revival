"use strict";

function collectionAccess(account, capabilities) {
  const authorized = account && account.state === "authorized";
  const unknown = !account || ["unknown", "failed"].includes(account.state);
  const result = {};
  for (const kind of ["daily", "liked", "favorites"]) {
    const available =
      capabilities &&
      capabilities[kind] &&
      capabilities[kind].available === true;
    result[kind] = {
      enabled: Boolean(authorized && available),
      reason: !authorized
        ? unknown
          ? "绑定状态未确认"
          : "未绑定网易云账号"
        : !capabilities
          ? "权限检测中"
          : !available
            ? "当前账号未提供此能力"
            : "",
    };
  }
  return result;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { collectionAccess };
} else {
  window.SpkMusicAccess = { collectionAccess };
}
