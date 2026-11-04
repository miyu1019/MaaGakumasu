using Newtonsoft.Json.Linq;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

namespace MFAAvalonia.Helper;

/// <summary>上游修正卡名时，把个人策略与分享文件中的旧卡名换成目录正式名；与 Agent 端 _hif_rename_cards 规则一致。</summary>
public static class HifCardRenames
{
    public static Dictionary<string, string> Load()
    {
        var path = HifLayout.CatalogPath("hif_card_renames.json");
        if (!File.Exists(path)) return [];
        return JObject.Parse(File.ReadAllText(path, Encoding.UTF8)).Properties()
            .Where(p => p.Value.Type == JTokenType.String).ToDictionary(p => p.Name, p => (string)p.Value!);
    }

    /// <summary>就地改名，返回是否有改动；新旧名并存时保留已有的新名条目。</summary>
    public static bool Apply(JToken token, IReadOnlyDictionary<string, string>? renames = null)
    {
        renames ??= Load();
        if (renames.Count == 0) return false;
        var changed = false;
        switch (token)
        {
            case JObject obj:
                foreach (var property in obj.Properties().ToList())
                {
                    changed |= Apply(property.Value, renames);
                    if (!renames.TryGetValue(property.Name, out var name)) continue;
                    if (obj.ContainsKey(name)) property.Remove();
                    else property.Replace(new JProperty(name, property.Value));
                    changed = true;
                }
                break;
            case JArray array:
                var kept = array.Select(Identity).Where(id => id != null && !renames.ContainsKey(id)).ToHashSet();
                foreach (var item in array.ToList())
                {
                    var id = Identity(item);
                    if (id != null && renames.TryGetValue(id, out var name) && kept.Contains(name))
                    {
                        item.Remove();
                        changed = true;
                    }
                    else changed |= Apply(item, renames);
                }
                break;
            case JValue { Type: JTokenType.String } value when renames.TryGetValue((string)value!, out var renamed):
                value.Value = renamed;
                changed = true;
                break;
        }
        return changed;
    }

    // 优先级条目以 key 区分，优先获取条目以 name 区分；其余列表元素按字符串本身。
    private static string? Identity(JToken item) => item switch
    {
        JObject obj => Text(obj["key"]) is { Length: > 0 } key ? key : Text(obj["name"]),
        _ => Text(item)
    };

    private static string? Text(JToken? token) => token is JValue { Type: JTokenType.String } value ? (string?)value : null;
}
