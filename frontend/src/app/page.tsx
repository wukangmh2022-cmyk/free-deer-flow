import { Header } from "@/components/landing/header";
import { Hero } from "@/components/landing/hero";

export default function LandingPage() {
  return (
    <div className="bg-background text-foreground min-h-screen w-full">
      <Header
        minimal
        className="bg-background/88 supports-[backdrop-filter]:bg-background/72"
      />
      <main className="flex w-full flex-col">
        <Hero />
      </main>
    </div>
  );
}
