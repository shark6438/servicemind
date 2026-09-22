import { z } from "zod";

const publicConfigSchema = z.object({
  apiUrl: z.url(),
  keycloakUrl: z.url(),
  keycloakRealm: z.string().min(1),
  keycloakClientId: z.string().min(1),
});

export const publicConfig = publicConfigSchema.parse({
  apiUrl: process.env.NEXT_PUBLIC_SERVICEMIND_API_URL ?? "http://127.0.0.1:18080",
  keycloakUrl: process.env.NEXT_PUBLIC_KEYCLOAK_URL ?? "http://127.0.0.1:8090",
  keycloakRealm: process.env.NEXT_PUBLIC_KEYCLOAK_REALM ?? "servicemind",
  keycloakClientId: process.env.NEXT_PUBLIC_KEYCLOAK_CLIENT_ID ?? "servicemind-api",
});
