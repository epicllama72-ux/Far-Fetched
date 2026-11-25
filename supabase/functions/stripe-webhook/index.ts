// Records a Press Pass payment. Stripe calls this after a Checkout completes; the signature is checked with STRIPE_WEBHOOK_SECRET,
// and the row is written with the service role (the browser can never write to `purchases`).
import { createClient } from "jsr:@supabase/supabase-js@2";

const enc = new TextEncoder();
const hex = (b: ArrayBuffer) => [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");

async function validSignature(body: string, header: string, secret: string): Promise<boolean> {
  const parts = Object.fromEntries(header.split(",").map((p) => p.split("=", 2) as [string, string]));
  const t = parts["t"], v1 = header.split(",").filter((p) => p.startsWith("v1=")).map((p) => p.slice(3));
  if (!t || !v1.length) return false;
  if (Math.abs(Date.now() / 1000 - Number(t)) > 300) return false;       // replayed or very old
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const expected = hex(await crypto.subtle.sign("HMAC", key, enc.encode(`${t}.${body}`)));
  return v1.some((s) => s.length === expected.length && s === expected);
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });
  const secret = Deno.env.get("STRIPE_WEBHOOK_SECRET");
  if (!secret) return new Response("not configured", { status: 500 });
  const body = await req.text();
  if (!(await validSignature(body, req.headers.get("stripe-signature") ?? "", secret))) {
    return new Response("bad signature", { status: 400 });
  }
  const event = JSON.parse(body);
  if (event.type !== "checkout.session.completed" && event.type !== "checkout.session.async_payment_succeeded") {
    return new Response("ignored", { status: 200 });
  }
  const s = event.data.object;
  if (s.payment_status !== "paid") return new Response("not paid yet", { status: 200 });
  const uid = String(s.client_reference_id ?? "");
  if (!/^[0-9a-f-]{36}$/i.test(uid)) return new Response("no player on this payment", { status: 200 });
  const db = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
  const { error } = await db.from("purchases").upsert(
    { user_id: uid, stripe_session: s.id, amount: s.amount_total ?? 0, currency: s.currency ?? "usd" },
    { onConflict: "stripe_session" },
  );
  if (error) return new Response("db error", { status: 500 });             // Stripe retries on a 500
  return new Response("ok", { status: 200 });
});
