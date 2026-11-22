import { redirect } from "next/navigation";

// The Live tab was renamed to Agent.
export default function LiveRedirect() {
  redirect("/agent");
}
