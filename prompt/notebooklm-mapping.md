# Generate NotebookLM Source Metadata

During NotebookLM processing, check whether the project contains the following metadata file:

```text
metadata/
└── sources.metadata.json
```

The `sources.metadata.json` file maps each processed source metadata filename to its corresponding NotebookLM `source_id`.

## Expected Format

The file must be valid JSON using the following structure:

```json
{
    "{metadata.json}": {
        "id": "source_id"
    }
}
```

For example:

```json
{
    "心经_en.txt metadata.json": {
        "id": "1401cb87-2d31-4fa9-8b10-32951a5c8948"
    },
    "朱子家训_en.txt metadata.json": {
        "id": "1401cb87-2d31-4fa9-8b10-32951a5c8948"
    }
}
```

## Processing Rules

When processing a NotebookLM project:

1. Check whether the `metadata` directory exists.

2. Check whether the following file exists:

   ```text
   metadata/sources.metadata.json
   ```

3. If `sources.metadata.json` exists:
   - Load and parse the JSON.
   - Verify that it is valid JSON.
   - Verify that every entry follows this structure:

     ```json
     {
         "<metadata filename>": {
             "id": "<source_id>"
         }
     }
     ```

   - Use the `id` value as the NotebookLM source ID associated with that source.

4. If `sources.metadata.json` does **not** exist:
   - Do not silently continue if NotebookLM source IDs are required for the requested operation.
   - Inform the user that the metadata mapping is missing.
   - Ask whether they would like to generate `metadata/sources.metadata.json`.

5. If the user chooses to generate the file:
   - Determine the metadata/source files that require NotebookLM source IDs.
   - Obtain the corresponding `source_id` for each source.
   - Create the `metadata` directory if it does not already exist.
   - Generate:

     ```text
     metadata/sources.metadata.json
     ```

   - Use the following structure:

     ```json
     {
         "<metadata filename>": {
             "id": "<source_id>"
         }
     }
     ```

6. Preserve Unicode filenames exactly as they appear. Do not translate, normalize, rename, or otherwise modify filenames such as:

   ```text
   心经_en.txt metadata.json
   朱子家训_en.txt metadata.json
   ```

7. Do not invent or generate fake `source_id` values. A `source_id` must come from the actual NotebookLM source information.

8. If a source ID cannot be determined, report the affected filename and ask the user to provide or retrieve the correct source ID rather than inserting a placeholder into the final file.

## Missing-File Prompt

If `metadata/sources.metadata.json` is missing, display a prompt similar to:

> `metadata/sources.metadata.json` was not found. This file maps source metadata files to their NotebookLM source IDs and may be required for this operation.
>
> Would you like to generate it now?

If the user agrees, generate the file in this format:

```json
{
    "{metadata.json}": {
        "id": "source_id"
    }
}
```

For multiple sources:

```json
{
    "<metadata-file-1>": {
        "id": "<source-id-1>"
    },
    "<metadata-file-2>": {
        "id": "<source-id-2>"
    }
}
```

## Important

`sources.metadata.json` is a mapping file. The JSON key represents the relevant metadata filename, while `id` represents the actual NotebookLM source ID.

Never fabricate a source ID merely to make the file syntactically complete.