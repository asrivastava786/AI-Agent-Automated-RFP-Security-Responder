import { auth } from "@/lib/auth";
import { redirect } from "next/navigation";
import { signOut } from "@/lib/auth";
import { ShieldCheck, LayoutDashboard, Upload, Settings, LogOut } from "lucide-react";
import Link from "next/link";

export default async function DashboardLayout({ children }: { children: React.ReactNode }) {
  const session = await auth();
  if (!session) redirect("/login");

  return (
    <div className="flex h-screen bg-gray-50">
      {/* Sidebar */}
      <aside className="w-60 bg-slate-900 flex flex-col">
        {/* Logo */}
        <div className="flex items-center gap-3 px-5 py-5 border-b border-slate-700">
          <div className="w-8 h-8 bg-indigo-600 rounded-lg flex items-center justify-center shrink-0">
            <ShieldCheck className="w-5 h-5 text-white" />
          </div>
          <div className="min-w-0">
            <p className="text-white font-semibold text-sm truncate">RFP Responder</p>
            <p className="text-slate-400 text-xs truncate">{session.user.tenantId}</p>
          </div>
        </div>

        {/* Nav */}
        <nav className="flex-1 px-3 py-4 space-y-1">
          <NavItem href="/"          icon={<LayoutDashboard className="w-4 h-4" />} label="Dashboard" />
          <NavItem href="/upload"    icon={<Upload className="w-4 h-4" />}          label="New Questionnaire" />
          <NavItem href="/admin/sso" icon={<Settings className="w-4 h-4" />}        label="SSO Settings" />
        </nav>

        {/* User */}
        <div className="px-3 py-4 border-t border-slate-700">
          <div className="flex items-center gap-3 px-2 py-2 rounded-lg">
            <div className="w-8 h-8 rounded-full bg-indigo-500 flex items-center justify-center text-white text-xs font-bold shrink-0">
              {session.user.email?.[0]?.toUpperCase() ?? "?"}
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-white text-xs font-medium truncate">{session.user.email}</p>
              <p className="text-slate-400 text-xs">
                {session.user.provider === "saml" ? "SSO" : "Password"}
              </p>
            </div>
            <form action={async () => { "use server"; await signOut({ redirectTo: "/login" }); }}>
              <button type="submit" className="text-slate-400 hover:text-white transition-colors">
                <LogOut className="w-4 h-4" />
              </button>
            </form>
          </div>
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-auto">
        {children}
      </main>
    </div>
  );
}

function NavItem({ href, icon, label }: { href: string; icon: React.ReactNode; label: string }) {
  return (
    <Link
      href={href}
      className="flex items-center gap-3 px-3 py-2 rounded-lg text-slate-300 hover:bg-slate-800 hover:text-white text-sm font-medium transition-colors"
    >
      {icon}
      {label}
    </Link>
  );
}
