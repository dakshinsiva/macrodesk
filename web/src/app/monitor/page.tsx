import type { Metadata } from "next";
import { Monitor } from "@/components/Monitor";

export const metadata: Metadata = {
  title: "Monitor",
  description: "The Treasury curve now vs a year ago, recession-risk gauges, and state unemployment, from FRED.",
};

export default function Page() {
  return <Monitor />;
}
