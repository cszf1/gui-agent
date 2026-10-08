using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

static class Launcher {
  [STAThread] static void Main() {
    try {
      string root = AppDomain.CurrentDomain.BaseDirectory;
      var start = new ProcessStartInfo(Path.Combine(root, "runtime", "pythonw.exe"), "-B -m gua.windows_app");
      start.WorkingDirectory = root;
      start.UseShellExecute = false;
      start.CreateNoWindow = true;
      start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
      Process.Start(start);
    } catch {
      MessageBox.Show("GUI Agent 无法启动，请重新安装完整安装包。", "GUI Agent", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }
  }
}
