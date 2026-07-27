import type { DefaultSession } from "next-auth";

declare module "next-auth" {
  interface Session {
    user: {
      /** The ALLOWED_EMAIL check, re-evaluated on every token refresh. */
      authorised?: boolean;
    } & DefaultSession["user"];
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    authorised?: boolean;
  }
}
