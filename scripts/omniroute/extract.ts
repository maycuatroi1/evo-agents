// Entry of the bundle scripts/generate_model_catalog.py builds with esbuild against a checkout of OmniRoute, then runs
// with node: prints, as JSON, one row per API-key and local provider, joining the registry entry of the gateway
// (open-sse/config/providers) with the display constants (src/shared/constants/providers). ROOT is replaced with the
// checkout's path before the bundle is built. The filtering and the shape of the catalog are in Python.
import { REGISTRY } from "ROOT/open-sse/config/providers/index.ts";
import * as P from "ROOT/src/shared/constants/providers.ts";
import { APIKEY_PROVIDERS_BEATAPI } from "ROOT/src/shared/constants/providers/apikey/beatapi.ts";
import { APIKEY_PROVIDERS_ENTERPRISE } from "ROOT/src/shared/constants/providers/apikey/enterprise-cloud.ts";
import { APIKEY_PROVIDERS_FRONTIER } from "ROOT/src/shared/constants/providers/apikey/frontier-labs.ts";
import { APIKEY_PROVIDERS_GATEWAYS } from "ROOT/src/shared/constants/providers/apikey/gateways.ts";
import { APIKEY_PROVIDERS_INFERENCE } from "ROOT/src/shared/constants/providers/apikey/inference-hosts.ts";
import { APIKEY_PROVIDERS_REGIONAL } from "ROOT/src/shared/constants/providers/apikey/regional.ts";
import { APIKEY_PROVIDERS_SPECIALTY } from "ROOT/src/shared/constants/providers/apikey/specialty-media.ts";

const FAMILIES: Record<string, Record<string, any>> = {
  gateways: APIKEY_PROVIDERS_GATEWAYS,
  beatapi: APIKEY_PROVIDERS_BEATAPI,
  frontier: APIKEY_PROVIDERS_FRONTIER,
  inference: APIKEY_PROVIDERS_INFERENCE,
  enterprise: APIKEY_PROVIDERS_ENTERPRISE,
  regional: APIKEY_PROVIDERS_REGIONAL,
  specialty: APIKEY_PROVIDERS_SPECIALTY,
  local: P.LOCAL_PROVIDERS,
};
const DISPLAY = [
  "alias", "name", "color", "textIcon", "website", "authHint", "apiHint", "apiKeyUrl", "freeNote", "hasFree",
  "serviceKinds", "deprecated", "hiddenFromDashboard", "subscriptionRisk", "noAuth", "browserBacked", "isLocalCli",
  "localDefault",
];
const REGISTERED = ["alias", "baseUrl", "format", "authType", "executor", "chatPath", "urlSuffix"];

function pick(from: any, keys: string[]) {
  return Object.fromEntries(keys.filter((k) => from?.[k] !== undefined).map((k) => [k, from[k]]));
}

const sets: Record<string, Set<string>> = {
  aggregator: P.AGGREGATOR_PROVIDER_IDS,
  enterprise: P.ENTERPRISE_CLOUD_PROVIDER_IDS,
  imageOnly: P.IMAGE_ONLY_PROVIDER_IDS,
  video: P.VIDEO_PROVIDER_IDS,
};
const rows: any[] = [];
for (const [family, providers] of Object.entries(FAMILIES)) {
  for (const [key, shown] of Object.entries(providers)) {
    const id = shown.id ?? key;
    const entry: any = (REGISTRY as any)[id];
    rows.push({
      family,
      id,
      display: pick(shown, DISPLAY),
      registry: entry ? { ...pick(entry, REGISTERED), defaultModel: entry.models?.[0]?.id ?? "" } : null,
      sets: Object.keys(sets).filter((name) => sets[name].has(id)),
    });
  }
}
console.log(JSON.stringify(rows));
