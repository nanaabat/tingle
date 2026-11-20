using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json;
using Rhino;
using Rhino.Commands;
using Rhino.Geometry;

namespace TinkerRhino
{
    /// <summary>
    /// Hunch is folded into the existing TinkerRhino plug-in (no new plug-in).
    /// HunchRead writes a building snapshot from the current document and, when
    /// HUNCH_PYTHON is set, runs the same hunch worker used by the Blender add-on.
    /// This command compiles against RhinoCommon; it has not been run inside Rhino.
    /// </summary>
    public sealed class HunchReadCommand : Command
    {
        public override string EnglishName => "TingleRead";

        protected override Result RunCommand(RhinoDoc doc, RunMode mode)
        {
            var snap = HunchSnapshot.FromDocument(doc, "", "calm");
            var dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".hunch", "rhino");
            Directory.CreateDirectory(dir);
            var input = Path.Combine(dir, "snapshot.json");
            var output = Path.Combine(dir, "result.json");
            File.WriteAllText(input, JsonSerializer.Serialize(snap, new JsonSerializerOptions { WriteIndented = true }));
            var py = Environment.GetEnvironmentVariable("HUNCH_PYTHON");
            if (string.IsNullOrWhiteSpace(py) || !File.Exists(py))
            {
                RhinoApp.WriteLine("hunch snapshot written to {0}. set HUNCH_PYTHON to run the worker.", input);
                return Result.Success;
            }
            var args = "-m hunch_adapter.worker --input \"" + input + "\" --output \"" + output + "\"";
            var root = Environment.GetEnvironmentVariable("HUNCH_ROOT");
            if (!string.IsNullOrWhiteSpace(root))
                args += " --hunch-root \"" + root + "\"";
            try
            {
                var psi = new ProcessStartInfo(py, args) { UseShellExecute = false, RedirectStandardError = true, RedirectStandardOutput = true };
                using var proc = Process.Start(psi);
                if (proc == null) { RhinoApp.WriteLine("hunch worker failed to start"); return Result.Failure; }
                proc.WaitForExit();
                if (proc.ExitCode != 0)
                {
                    RhinoApp.WriteLine("hunch worker failed: {0}", proc.StandardError.ReadToEnd());
                    return Result.Failure;
                }
                RhinoApp.WriteLine("hunch result written to {0}", output);
                return Result.Success;
            }
            catch (Exception ex)
            {
                RhinoApp.WriteLine("hunch worker could not run: {0}", ex.Message);
                return Result.Failure;
            }
        }
    }

    internal static class HunchSnapshot
    {
        static readonly string[] OpeningWords = { "window", "glaz", "glass", "opening", "sash", "fenestr" };

        public static Dictionary<string, object?> FromDocument(RhinoDoc doc, string site, string target)
        {
            var faces = new List<Dictionary<string, object?>>();
            var rooms3d = new List<Dictionary<string, object?>>();
            foreach (var obj in doc.Objects)
            {
                Mesh? m = obj.Geometry as Mesh;
                if (m == null && obj.Geometry is Brep brep)
                {
                    var meshes = Mesh.CreateFromBrep(brep, MeshingParameters.Default);
                    if (meshes == null || meshes.Length == 0) continue;
                    m = new Mesh();
                    foreach (var part in meshes) m.Append(part);
                }
                if (m == null || m.Vertices.Count == 0 || m.Faces.Count == 0) continue;
                var layer = (obj.Attributes.LayerIndex >= 0 && obj.Attributes.LayerIndex < doc.Layers.Count) ? doc.Layers[obj.Attributes.LayerIndex] : null;
                var lname = layer != null ? layer.FullPath : (obj.Name ?? "layer");
                var isOpen = OpeningWords.Any(w => (lname + " " + (obj.Name ?? "")).ToLowerInvariant().Contains(w));
                var isRoom = obj.Attributes.GetUserString("hunch_room_shell") == "1" || lname.ToLowerInvariant().Contains("room");
                if (isRoom)
                {
                    var bbox = m.GetBoundingBox(true);
                    rooms3d.Add(new Dictionary<string, object?>
                    {
                        ["name"] = string.IsNullOrWhiteSpace(obj.Name) ? lname : obj.Name,
                        ["min"] = new[] { bbox.Min.X, bbox.Min.Y, bbox.Min.Z },
                        ["max"] = new[] { bbox.Max.X, bbox.Max.Y, bbox.Max.Z },
                        ["source"] = "rhino closed mesh",
                    });
                }
                m.Faces.ConvertQuadsToTriangles();
                var verts = m.Vertices;
                for (int i = 0; i < m.Faces.Count; i++)
                {
                    var f = m.Faces[i];
                    var a = verts[f.A]; var b = verts[f.B]; var c = verts[f.C];
                    var n = Vector3d.CrossProduct(new Vector3d(b - a), new Vector3d(c - a));
                    var area = 0.5 * n.Length;
                    if (area < 1e-12) continue;
                    n.Unitize();
                    var mid = new Point3d((a.X + b.X + c.X) / 3.0, (a.Y + b.Y + c.Y) / 3.0, (a.Z + b.Z + c.Z) / 3.0);
                    faces.Add(new Dictionary<string, object?>
                    {
                        ["verts"] = new[] { new[] { a.X, a.Y, a.Z }, new[] { b.X, b.Y, b.Z }, new[] { c.X, c.Y, c.Z } },
                        ["area"] = area,
                        ["normal"] = new[] { n.X, n.Y, n.Z },
                        ["room"] = RoomOf(obj, mid, rooms3d),
                        ["material"] = lname,
                        ["kind"] = isOpen ? "opening" : KindFromNormal(n, lname),
                    });
                }
            }
            return new Dictionary<string, object?>
            {
                ["up_axis"] = 2,
                ["faces"] = faces,
                ["materials"] = new Dictionary<string, object?>(),
                ["rooms3d"] = rooms3d,
                ["path"] = doc.Path ?? "rhino-document",
                ["site"] = site,
                ["target"] = target,
                ["orient_deg"] = 0,
                ["host"] = new Dictionary<string, object?> { ["name"] = "rhino", ["version"] = "Rhino 8" },
            };
        }

        static string KindFromNormal(Vector3d n, string? name)
        {
            var low = (name ?? "").ToLowerInvariant();
            if (OpeningWords.Any(low.Contains)) return "opening";
            if (n.Z > 0.85 || low.Contains("floor")) return "floor";
            if (n.Z < -0.85 || low.Contains("ceil") || low.Contains("roof")) return "ceiling";
            if (Math.Abs(n.Z) < 0.35) return "wall";
            return n.Z < 0 ? "slope" : "other";
        }

        static string RoomOf(Rhino.DocObjects.RhinoObject obj, Point3d mid, List<Dictionary<string, object?>> rooms)
        {
            var named = obj.Attributes.GetUserString("hunch_room");
            if (!string.IsNullOrWhiteSpace(named)) return named;
            foreach (var r in rooms)
            {
                var mn = (double[])r["min"]!;
                var mx = (double[])r["max"]!;
                if (mid.X >= mn[0] - 0.15 && mid.X <= mx[0] + 0.15
                    && mid.Y >= mn[1] - 0.15 && mid.Y <= mx[1] + 0.15
                    && mid.Z >= mn[2] - 0.15 && mid.Z <= mx[2] + 0.15)
                    return (string)r["name"]!;
            }
            return string.IsNullOrWhiteSpace(obj.Name) ? "_unassigned" : obj.Name;
        }
    }
}
